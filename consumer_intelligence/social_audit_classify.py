"""Per-article classification for the Social Audit lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-audit.md

The source deck audits three fixed research pillars ("Kids, Parenting &
Devices", "...& AI", "...& Screentime") — these are the audit's own scope
boundaries, not a dataset-driven taxonomy, so they're the one thing this
module hardcodes. Everything *within* a pillar (its recurring themes) is
left to the generic `taxonomy` module, scoped to that pillar's own rows —
same "computed, not hardcoded" split as every other CI lens.

One batched, temperature-0 LLM pass routes every post to at most one
pillar. Fallback is keyword rules so the lens still builds (with `none` for
posts the rules can't place) when the LLM is unavailable.
"""

import asyncio
import logging
import re

from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PREPARE_KEY = "social_audit"

PILLAR_KEYS = ("devices", "ai", "screentime")
PILLAR_TITLES = {
    "devices": "Kids, Parenting & Devices",
    "ai": "Kids, Parenting & AI",
    "screentime": "Kids, Parenting & Screentime",
}
PILLAR_DEFS = {
    "devices": "Tablets, e-readers, voice assistants, parental controls, kid-friendly hardware and digital reading.",
    "ai": "AI chatbots, AI in homework/education, AI literacy, AI safety/privacy, AI-enabled harm to children.",
    "screentime": "Screen-time limits, screen-free routines, balanced/intentional screen use, developmental impact of screens.",
}

BATCH = 60
CONCURRENCY = 3

_PILLAR_RULES = (
    ("ai", re.compile(r"\bai\b|artificial intelligence|chatbot|chatgpt|character\.ai|ai tutor|ai literacy|deepfake", re.I)),
    ("devices", re.compile(r"tablet|ipad|kindle|e-?reader|e-?book|alexa|echo|voice assistant|parental control|fire tablet", re.I)),
    ("screentime", re.compile(r"screen ?time|screen-free|scrolling|screen limit|device time|screen use|screen dependency", re.I)),
)


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def _brief(a: dict, chars: int = 260) -> dict:
    return {
        "id": article_id(a),
        "title": str(a.get("title") or "")[:140],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "theme": str(a.get("theme") or "")[:60],
    }


def fallback_label(a: dict) -> dict:
    text = f"{a.get('theme') or ''} {a.get('title') or ''} {a.get('summary') or ''}"
    for key, rx in _PILLAR_RULES:
        if rx.search(text):
            return {"pillar": key}
    return {"pillar": "none"}


async def _label_batch(briefs: list[dict], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "pillars": PILLAR_DEFS,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "pillar": "|".join(PILLAR_KEYS) + "|none — the single best-fit research pillar, or none if the post "
                       "doesn't concern kids/parenting/technology at all",
        }]},
    }
    system = (
        "You route consumer social posts about kids, parenting and technology into exactly one research pillar "
        "for a social-audit dashboard. Judge from the text only. Use 'none' for posts outside all three pillars. "
        "Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Route these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=180,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        pillar = str(it.get("pillar") or "none").lower()
        out[str(it["id"])] = {"pillar": pillar if pillar in PILLAR_KEYS else "none"}
    return out


async def label_articles(articles: list[dict]) -> dict:
    briefs = [_brief(a) | {"id": article_id(a)} for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Social audit label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        by_id.setdefault(b["id"], fallback_label(by_article[b["id"]]))
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels for every post. Never raises."""
    labels = await label_articles(articles)
    return {"labels": labels}


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {"pillar": "none"}
