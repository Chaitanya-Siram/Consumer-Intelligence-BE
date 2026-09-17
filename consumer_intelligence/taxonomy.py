"""Theme taxonomy for Consumer Intelligence lenses.

The tagger emits a long tail of free-text `theme` labels (100+ distinct values
on a 400-article session). Charts need <= 8 honest rows. This module asks the
LLM once per build to group the raw labels into canonical groups, then code
counts articles on those groups. The LLM never produces a number; it only
supplies the grouping and, per group, which loyalty parameter bucket the group
speaks to (used by the Shifting Audience Priorities index).

The mapping is returned so the payload can carry it under `meta.taxonomy`
and every chart row can be traced back to the raw tags it aggregates.

On any LLM failure the deterministic fallback keeps the top groups as-is and
folds the tail into "Others".
"""

import asyncio
import logging
from collections import Counter

from . import aggregate
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

OTHERS = "Others"
MAX_GROUPS = 8
MAX_RAW_TO_LLM = 60          # long tail beyond this goes straight to Others
BUCKETS = ("advocacy", "sentiment", "usage", "switching", "other")
GROUP_FIELD = "theme_group"

_SYSTEM = (
    "You are a consumer-intelligence analyst organising a tagging taxonomy. "
    "You will receive raw theme labels with mention counts from news and social coverage "
    "about a brand and its category. Group them into a small set of canonical themes a "
    "brand dashboard can chart. Rules: every raw label appears in exactly one group; group "
    "names are short noun phrases (2-4 words) in Title Case; do not invent labels that are "
    "not in the input; prefer merging near-duplicates and singletons into a broader group "
    "over creating tiny groups. For each group also pick the loyalty parameter it most "
    "informs: advocacy (recommendation, awards, endorsement, trust), sentiment (quality, "
    "performance, satisfaction, complaints), usage (how/when/how often the product is used, "
    "integration, routines), switching (comparisons, alternatives, promotions, price, "
    "multi-brand consideration), or other. Return ONLY JSON."
)


def raw_counts(articles: list[dict], field: str = "theme") -> Counter:
    counts: Counter = Counter()
    for a in articles:
        v = a.get(field)
        if v is None or v == "" or aggregate.is_junk(v):
            continue
        counts[str(v).strip()] += 1
    return counts


def fallback(counts: Counter, max_groups: int = MAX_GROUPS) -> dict:
    keep = [name for name, _ in counts.most_common(max_groups - 1)]
    groups = [{"name": n, "raw": [n], "bucket": "other"} for n in keep]
    tail = [n for n in counts if n not in keep]
    if tail:
        groups.append({"name": OTHERS, "raw": sorted(tail), "bucket": "other"})
    return _finish(groups, counts, method="fallback")


def _finish(groups: list[dict], counts: Counter, *, method: str) -> dict:
    mapping: dict[str, str] = {}
    for g in groups:
        for raw in g["raw"]:
            mapping.setdefault(raw, g["name"])
    for g in groups:
        g["count"] = sum(counts.get(r, 0) for r in g["raw"])
    groups = [g for g in groups if g["count"] > 0]
    groups.sort(key=lambda g: (g["name"] == OTHERS, -g["count"], g["name"]))
    return {"groups": groups, "map": mapping, "method": method, "raw_distinct": len(counts)}


def _validate(raw: dict, counts: Counter, max_groups: int) -> list[dict] | None:
    items = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        return None
    groups: list[dict] = []
    assigned: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        members = [str(m).strip() for m in (item.get("raw") or []) if str(m).strip() in counts and str(m).strip() not in assigned]
        if not name or not members:
            continue
        bucket = str(item.get("bucket") or "other").strip().lower()
        groups.append({"name": name[:48], "raw": members, "bucket": bucket if bucket in BUCKETS else "other"})
        assigned.update(members)
    if not groups:
        return None
    # Cap the group count: merge the smallest into Others.
    groups.sort(key=lambda g: -sum(counts[r] for r in g["raw"]))
    if len(groups) > max_groups:
        head, tail = groups[: max_groups - 1], groups[max_groups - 1 :]
        groups = head + [{"name": OTHERS, "raw": sorted(r for g in tail for r in g["raw"]), "bucket": "other"}]
    leftover = sorted(set(counts) - {r for g in groups for r in g["raw"]})
    if leftover:
        others = next((g for g in groups if g["name"] == OTHERS), None)
        if others:
            others["raw"] = sorted(set(others["raw"]) | set(leftover))
        else:
            groups.append({"name": OTHERS, "raw": leftover, "bucket": "other"})
    return groups


async def canonicalize(
    articles: list[dict],
    *,
    field: str = "theme",
    max_groups: int = MAX_GROUPS,
    brand: str = "",
    timeout: float = 90.0,
) -> dict:
    """{"groups": [{"name","raw","bucket","count"}], "map": {raw: group}, "method"}.

    Never raises. Falls back to top-N + Others on any failure or when there are
    already few enough distinct labels to chart directly."""
    counts = raw_counts(articles, field)
    if not counts:
        return {"groups": [], "map": {}, "method": "empty", "raw_distinct": 0}
    if len(counts) <= max_groups:
        groups = [{"name": n, "raw": [n], "bucket": "other"} for n in counts]
        return _finish(groups, counts, method="direct")

    head = counts.most_common(MAX_RAW_TO_LLM)
    tail = [n for n in counts if n not in {h for h, _ in head}]
    payload = {
        "brand": brand or None,
        "max_groups": max_groups - (1 if tail else 0),
        "raw_themes": [{"label": n, "count": c} for n, c in head],
        "schema": {"groups": [{"name": "str", "raw": ["labels from input"], "bucket": "|".join(BUCKETS)}]},
    }
    try:
        client = get_narrative_client()
        raw = await asyncio.wait_for(
            client.complete_json(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"Group these raw themes.\n\nINPUT:\n{payload}"},
                ],
                temperature=0.0,
            ),
            timeout=timeout,
        )
        groups = _validate(raw, Counter(dict(head)), max_groups - (1 if tail else 0))
        if not groups:
            raise ValueError("LLM taxonomy returned no usable groups")
        if tail:
            others = next((g for g in groups if g["name"] == OTHERS), None)
            if others:
                others["raw"] = sorted(set(others["raw"]) | set(tail))
            else:
                groups.append({"name": OTHERS, "raw": sorted(tail), "bucket": "other"})
        return _finish(groups, counts, method="llm")
    except Exception as exc:
        logger.warning("CI taxonomy fell back to top-N: %s", exc)
        return fallback(counts, max_groups)


def annotate(articles: list[dict], taxonomy: dict, *, field: str = "theme") -> list[dict]:
    """Set `theme_group` on each article in place (Others when unmapped/junk)."""
    mapping = taxonomy.get("map") or {}
    for a in articles:
        v = a.get(field)
        key = str(v).strip() if v is not None else ""
        a[GROUP_FIELD] = mapping.get(key, OTHERS) if key and not aggregate.is_junk(key) else OTHERS
    return articles


def group_counts(articles: list[dict]) -> Counter:
    return Counter(a.get(GROUP_FIELD) or OTHERS for a in articles)


def group_bucket(taxonomy: dict) -> dict[str, str]:
    return {g["name"]: g.get("bucket", "other") for g in taxonomy.get("groups", [])}


def summary(taxonomy: dict) -> dict:
    """Compact form for `meta.taxonomy`: method + group -> raw labels."""
    return {
        "method": taxonomy.get("method"),
        "raw_distinct": taxonomy.get("raw_distinct"),
        "groups": [{"name": g["name"], "bucket": g.get("bucket"), "count": g.get("count"), "raw": g["raw"][:25]} for g in taxonomy.get("groups", [])],
    }
