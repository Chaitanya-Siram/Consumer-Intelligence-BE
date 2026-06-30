"""E2B sandbox runner for agent-generated chart code.

Executes untrusted, LLM-generated Python in an isolated E2B cloud sandbox with
the session's tagged articles pre-loaded as a pandas DataFrame named `df`. The
agent code must print a single JSON object as its final stdout line; that object
is parsed into the chart payload.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from configs import envs, logger


@dataclass
class SandboxResult:
    ok: bool
    data: dict[str, Any] | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    # True when the failure is in the agent code itself (a fix-and-retry might
    # help). False for infra failures (missing key, package, network) where
    # regenerating the code is pointless.
    retryable: bool = False


def _clean_nans(obj: Any) -> Any:
    """Recursively replace NaN / +-Infinity with None so the payload is strict-JSON
    compliant (Starlette serializes with allow_nan=False)."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_nans(v) for v in obj]
    return obj


# The agent code receives `df` already built and date-parsed. _raw is a JSON
# string literal (repr of json.dumps(...)) so embedding it can't break the source.
_WRAPPER = """\
import json
import pandas as pd

_raw = {data_literal}
df = pd.DataFrame(json.loads(_raw))
if 'date' in df.columns:
    # utc=True forces a real datetime64[ns, UTC] column even when the source
    # strings have mixed/zoned offsets (e.g. 'Z' or +05:30). Without it, pandas
    # falls back to an object-dtype Series and any `.dt` accessor on it raises
    # "Can only use .dt accessor with datetimelike values".
    df['date'] = pd.to_datetime(df['date'], errors='coerce', utc=True)

# --- agent-generated code ---
{agent_code}
"""


def run_chart_code(python_code: str, articles: list[dict[str, Any]]) -> SandboxResult:
    """Run `python_code` against the articles in a fresh E2B sandbox and return the
    parsed chart dict. Never raises — failures come back as SandboxResult(ok=False)."""
    if not envs.E2B_API_KEY:
        return SandboxResult(ok=False, error="E2B_API_KEY is not set; cannot run the code sandbox.")

    try:
        from e2b_code_interpreter import Sandbox
    except ImportError:
        return SandboxResult(
            ok=False,
            error="e2b-code-interpreter is not installed. Run `pip install e2b-code-interpreter`.",
        )

    data_literal = repr(json.dumps(articles, default=str))
    full_code = _WRAPPER.format(data_literal=data_literal, agent_code=python_code)

    try:
        with Sandbox.create(api_key=envs.E2B_API_KEY) as sbx:
            execution = sbx.run_code(full_code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("E2B sandbox execution failed")
        return SandboxResult(ok=False, error=f"Sandbox execution failed: {exc}")

    logs = getattr(execution, "logs", None)
    stdout = "\n".join(logs.stdout).strip() if logs and logs.stdout else ""
    stderr = "\n".join(logs.stderr).strip() if logs and logs.stderr else ""

    if getattr(execution, "error", None):
        err = execution.error
        detail = getattr(err, "value", None) or getattr(err, "name", None) or str(err)
        traceback = "\n".join(getattr(err, "traceback", "") or []) if isinstance(getattr(err, "traceback", None), list) else (getattr(err, "traceback", "") or "")
        message = f"Code raised: {detail}"
        if traceback:
            message = f"{message}\nTraceback:\n{traceback[-1500:]}"
        return SandboxResult(ok=False, stdout=stdout, stderr=stderr, error=message, retryable=True)

    if not stdout:
        return SandboxResult(
            ok=False, stdout=stdout, stderr=stderr, retryable=True,
            error=f"No stdout from sandbox (the code must end with print(json.dumps(result))). stderr: {stderr[:300]}",
        )

    json_lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
    if not json_lines:
        return SandboxResult(
            ok=False, stdout=stdout, stderr=stderr, retryable=True,
            error="Sandbox produced no JSON object on stdout (expected a final print(json.dumps(result))).",
        )

    try:
        data = json.loads(json_lines[-1])
    except json.JSONDecodeError as exc:
        return SandboxResult(ok=False, stdout=stdout, stderr=stderr, retryable=True, error=f"Failed to parse chart JSON: {exc}")

    return SandboxResult(ok=True, data=_clean_nans(data), stdout=stdout, stderr=stderr)
