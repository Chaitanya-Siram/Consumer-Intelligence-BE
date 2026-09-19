"""LLM audit run for the Congruence & Content Intelligence lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-congruence-content.md §2, §5.1.

A fixed prompt set about the brand and category is executed against every
configured assistant. Each response is stored with the sources the assistant
gives for it. The run is persisted on S3 beside the CI cache and reused for
RUN_TTL_DAYS unless the caller asks for a refresh.

Assistants and honesty about citations
--------------------------------------
Only assistants with a working key are run; the rest are listed under
`unavailable` with the reason, never simulated. Today's environment holds
Azure OpenAI (ChatGPT) and Anthropic (Claude) keys; Perplexity and Gemini
adapters switch on when PERPLEXITY_API_KEY / GEMINI_API_KEY are set.

  ChatGPT      Azure chat completions, no browsing -> citations are
               "model-claimed": the assistant lists the sources it would cite.
  Claude       Anthropic messages with the web_search server tool ->
               "grounded" citations when the tool runs.
  Perplexity   OpenAI-compatible chat with `citations` -> "grounded".
  Gemini       generateContent, no grounding here -> "model-claimed".

Every stored response records its `citation_mode` so the dashboard can say
which numbers rest on what the model asserted versus what it retrieved.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from configs import envs
from file_helpers.s3_file import s3_file

logger = logging.getLogger(__name__)

RUN_TTL_DAYS = int(os.getenv("LLM_AUDIT_TTL_DAYS", "7"))
CONCURRENCY = int(os.getenv("LLM_AUDIT_CONCURRENCY", "4"))
MAX_COMPETITORS_IN_PROMPTS = 3
DISPLAY = {"chatgpt": "ChatGPT", "gemini": "Gemini", "copilot": "Copilot", "claude": "Claude", "perplexity": "Perplexity", "meta ai": "Meta AI", "meta": "Meta AI"}
DEFAULT_ASSISTANTS = os.getenv("LLM_AUDIT_ASSISTANTS", "ChatGPT,Claude,Perplexity,Gemini")

_CACHE_SCOPE = os.getenv("CI_CACHE_SCOPE", "").strip().strip("/")

# ── prompt set ────────────────────────────────────────────────────────────
# id, template. {brand} {category} {competitor}. Competitor templates expand
# once per top competitor.
PROMPT_TEMPLATES = [
    ("known_for", "What is {brand} best known for in {category}?"),
    ("strengths_weaknesses", "What are the main strengths and weaknesses of {brand} {category} products?"),
    ("criticisms", "What criticisms or complaints do people have about {brand}?"),
    ("who_should_buy", "Who should buy {brand} products and why?"),
    ("worth_price", "Is {brand} worth the price compared with alternatives?"),
    ("best_products", "Which {brand} products are considered the best, and what makes them stand out?"),
    ("recent", "What recent news, launches or changes are there about {brand}?"),
    ("sources", "Which publications, reviewers or communities write most about {brand}, and what do they say?"),
    ("compare", "How does {brand} compare with {competitor} in {category}?"),
    ("category_best", "Which brands are the best in {category} right now, and why?"),
    ("category_trusted", "Which {category} brand do consumers trust most, and what drives that trust?"),
]
_ANSWER_INSTRUCTION = (
    "Answer as you normally would for a consumer. Then return ONLY a JSON object: "
    '{"answer": "<your full answer, 120-250 words>", '
    '"sources": [{"url": "<http(s) url>", "title": "<page or article title>", "outlet": "<publication or site name>", "author": "<byline if you know it, else empty>"}]} '
    "with 2-6 sources you would actually rely on for this answer. Do not invent URLs: if you are unsure of an exact URL, give the outlet's homepage."
)


def build_prompts(brand: str, category: str, competitors: list[str]) -> list[dict]:
    out = []
    comps = [c for c in competitors if c and c.lower() != (brand or "").lower()][:MAX_COMPETITORS_IN_PROMPTS]
    for pid, tpl in PROMPT_TEMPLATES:
        if "{competitor}" in tpl:
            for c in comps:
                out.append({"prompt_id": f"{pid}:{re.sub(r'[^a-z0-9]+', '-', c.lower()).strip('-')}", "kind": "competitor", "competitor": c, "text": tpl.format(brand=brand, category=category, competitor=c)})
        else:
            out.append({"prompt_id": pid, "kind": "category" if pid.startswith("category_") else "brand", "text": tpl.format(brand=brand, category=category)})
    return out


# ── assistant adapters ────────────────────────────────────────────────────


def _parse_answer(text: str) -> tuple[str, list[dict]]:
    """Best-effort extraction of {"answer","sources"} from a model reply."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    answer = str(data.get("answer") or (raw if not data else "")).strip()
    sources = []
    for s in data.get("sources") or []:
        if isinstance(s, dict) and str(s.get("url") or "").startswith(("http://", "https://")):
            sources.append({"url": str(s["url"]).strip(), "title": str(s.get("title") or "").strip()[:160], "outlet": str(s.get("outlet") or "").strip()[:80], "author": str(s.get("author") or "").strip()[:80]})
    return answer, sources


async def _ask_azure(prompt: str) -> tuple[str, list[dict], str]:
    import httpx

    url = f"{envs.AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/{envs.AZURE_OPENAI_MODEL}/chat/completions?api-version={envs.AZURE_OPENAI_API_VERSION}"
    body = {"messages": [{"role": "user", "content": f"{prompt}\n\n{_ANSWER_INSTRUCTION}"}], "temperature": 0.7, "response_format": {"type": "json_object"}}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers={"api-key": envs.AZURE_OPENAI_API_KEY, "Content-Type": "application/json"}, json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    answer, sources = _parse_answer(text)
    return answer, sources, "model-claimed"


async def _ask_claude(prompt: str) -> tuple[str, list[dict], str]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=envs.ANTHROPIC_API_KEY)
    r = await client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=1200,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": f"{prompt}\n\nAnswer for a consumer in 120-250 words and cite sources."}],
    )
    text_parts, cites = [], []
    for b in r.content:
        if b.type == "text":
            text_parts.append(b.text)
            for c in getattr(b, "citations", None) or []:
                url = getattr(c, "url", None)
                if url:
                    cites.append({"url": url, "title": getattr(c, "title", "") or "", "outlet": "", "author": ""})
    seen, sources = set(), []
    for c in cites:
        if c["url"] not in seen:
            seen.add(c["url"])
            sources.append(c)
    return "\n".join(text_parts).strip(), sources, "grounded" if sources else "model-claimed"


async def _ask_perplexity(prompt: str) -> tuple[str, list[dict], str]:
    import httpx

    key = os.getenv("PERPLEXITY_API_KEY", "")
    body = {"model": os.getenv("PERPLEXITY_MODEL", "sonar"), "messages": [{"role": "user", "content": f"{prompt}\n\nAnswer for a consumer in 120-250 words."}]}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post("https://api.perplexity.ai/chat/completions", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body)
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    sources = [{"url": u, "title": "", "outlet": "", "author": ""} for u in data.get("citations") or [] if str(u).startswith("http")]
    return text.strip(), sources, "grounded" if sources else "model-claimed"


async def _ask_gemini(prompt: str) -> tuple[str, list[dict], str]:
    import httpx

    key = os.getenv("GEMINI_API_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": f"{prompt}\n\n{_ANSWER_INSTRUCTION}"}]}], "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"}}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers={"Content-Type": "application/json"}, json=body)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    answer, sources = _parse_answer(text)
    return answer, sources, "model-claimed"


ADAPTERS = {
    "ChatGPT": (_ask_azure, lambda: bool(envs.AZURE_OPENAI_API_KEY and envs.AZURE_OPENAI_ENDPOINT and envs.AZURE_OPENAI_MODEL)),
    "Claude": (_ask_claude, lambda: bool(envs.ANTHROPIC_API_KEY)),
    "Perplexity": (_ask_perplexity, lambda: bool(os.getenv("PERPLEXITY_API_KEY"))),
    "Gemini": (_ask_gemini, lambda: bool(os.getenv("GEMINI_API_KEY"))),
    # Copilot and Meta AI have no public API in this environment.
}


def configured_assistants() -> tuple[list[str], dict[str, str]]:
    wanted = [DISPLAY.get(n.strip().lower(), n.strip()) for n in DEFAULT_ASSISTANTS.split(",") if n.strip()]
    available, unavailable = [], {}
    for name in wanted:
        entry = ADAPTERS.get(name)
        if not entry:
            unavailable[name] = "no API adapter in this environment"
        elif not entry[1]():
            unavailable[name] = "no API key configured"
        else:
            available.append(name)
    return available, unavailable


# ── source normalisation ──────────────────────────────────────────────────

_TYPE_RULES = (
    ("Reddit", re.compile(r"reddit\.com")),
    ("Video transcripts", re.compile(r"youtube\.com|youtu\.be|tiktok\.com")),
    ("Retailer sites", re.compile(r"amazon\.|walmart\.|autozone\.|advanceautoparts\.|oreillyauto\.|target\.|homedepot\.|lowes\.|ebay\.|bestbuy\.|costco\.")),
    ("News wires", re.compile(r"reuters\.|apnews\.|prnewswire\.|businesswire\.|globenewswire\.")),
    ("Forums & Q&A", re.compile(r"quora\.com|stackexchange|forum|forums\.|\.club\b")),
    ("Encyclopaedic", re.compile(r"wikipedia\.org")),
)


def outlet_from_url(url: str) -> tuple[str, str]:
    """(domain, display outlet name). Domain strips www; name is the registrable
    label, Title Cased, with a few well-known spellings kept."""
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return "", ""
    known = {"reddit.com": "Reddit", "youtube.com": "YouTube", "amazon.com": "Amazon", "walmart.com": "Walmart", "wikipedia.org": "Wikipedia", "consumerreports.org": "Consumer Reports", "caranddriver.com": "Car and Driver", "motortrend.com": "MotorTrend", "autoblog.com": "Autoblog", "thedrive.com": "The Drive", "roadandtrack.com": "Road & Track", "popularmechanics.com": "Popular Mechanics", "nytimes.com": "The New York Times", "wirecutter.com": "Wirecutter"}
    for dom, name in known.items():
        if host == dom or host.endswith("." + dom):
            return dom, name
    parts = host.split(".")
    label = parts[-3] if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} else (parts[-2] if len(parts) >= 2 else parts[0])
    return host, label.replace("-", " ").title()


def source_type(domain: str, brand: str, competitors: list[str]) -> str:
    d = domain.lower()
    names = [n for n in [brand, *competitors] if n]
    for n in names:
        slug = re.sub(r"[^a-z0-9]", "", n.lower())
        if slug and slug in d.replace("-", "").replace(".", ""):
            return "Brand & manufacturer sites"
    for label, rx in _TYPE_RULES:
        if rx.search(d):
            return label
    return "News & trade press"


# ── run ───────────────────────────────────────────────────────────────────


def _prefix(session_id: int) -> str:
    scope = f"{_CACHE_SCOPE}/" if _CACHE_SCOPE else ""
    return f"session_files/{scope}session_{session_id}/llm_audit"


def load_run(session_id: int) -> dict | None:
    try:
        raw = s3_file.download_file(f"{_prefix(session_id)}/latest.json")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def save_run(session_id: int, run: dict) -> None:
    try:
        blob = json.dumps(run).encode("utf-8")
        s3_file.upload_file(f"{_prefix(session_id)}/latest.json", blob)
        s3_file.upload_file(f"{_prefix(session_id)}/run_{int(time.time())}.json", blob)
    except Exception:
        logger.exception("Failed to persist LLM audit run for session_id=%s", session_id)


def run_is_fresh(run: dict | None, brand: str, category: str) -> bool:
    if not run or run.get("brand") != brand:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(run["run_at"])
    except Exception:
        return False
    return age.days < RUN_TTL_DAYS and bool(run.get("responses"))


async def execute_run(*, brand: str, category: str, competitors: list[str], assistants: list[str] | None = None) -> dict:
    """Run the prompt set across available assistants. Never raises; failed
    calls are recorded under `errors` and the assistant is dropped when every
    call to it failed."""
    available, unavailable = configured_assistants()
    if assistants:
        available = [a for a in available if a in assistants]
    prompts = build_prompts(brand, category, competitors)
    sem = asyncio.Semaphore(CONCURRENCY)
    errors: dict[str, list[str]] = {}

    async def one(assistant: str, prompt: dict) -> dict | None:
        fn = ADAPTERS[assistant][0]
        async with sem:
            try:
                answer, sources, mode = await asyncio.wait_for(fn(prompt["text"]), timeout=150)
            except Exception as exc:
                errors.setdefault(assistant, []).append(f"{prompt['prompt_id']}: {type(exc).__name__}: {str(exc)[:120]}")
                return None
        if not answer:
            errors.setdefault(assistant, []).append(f"{prompt['prompt_id']}: empty answer")
            return None
        norm = []
        for s in sources:
            dom, name = outlet_from_url(s["url"])
            if dom:
                norm.append({**s, "domain": dom, "outlet": s.get("outlet") or name, "type": source_type(dom, brand, competitors)})
        return {"llm": assistant, "prompt_id": prompt["prompt_id"], "kind": prompt["kind"], "competitor": prompt.get("competitor"), "prompt": prompt["text"], "text": answer, "citations": norm, "citation_mode": mode}

    started = time.time()
    results = await asyncio.gather(*(one(a, p) for a in available for p in prompts))
    responses = [r for r in results if r]
    ran = sorted({r["llm"] for r in responses}, key=available.index)
    for a in available:
        if a not in ran:
            unavailable[a] = "every call failed: " + (errors.get(a) or ["unknown"])[0][:100]
    return {
        "brand": brand,
        "category": category,
        "competitors": competitors,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 1),
        "assistants": ran,
        "unavailable": unavailable,
        "prompts": prompts,
        "responses": responses,
        "errors": errors,
    }
