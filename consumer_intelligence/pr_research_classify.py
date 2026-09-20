"""Per-article classification for the PR Research lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-pr-research.md

The source deck profiles editorial *audience segments* — who an article's
angle is actually written for (a practical filer, a cost-conscious reader,
a policy wonk, a trade professional, an equity/advocacy reader). That's a
different signal than `taxonomy`'s topic themes (what the article discusses),
so it gets its own small fixed vocabulary, generic enough to apply to any
PR/editorial topic rather than this one deck's tax-filing-specific segment
names. One batched, temperature-0 LLM pass labels every article. Fallback is
keyword rules so the lens still builds when the LLM is unavailable.
"""

import asyncio
import logging
import re

from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PREPARE_KEY = "pr_research"

SEGMENT_KEYS = ("practical_service", "value_conscious", "policy_engaged", "industry_trade", "advocacy_equity")
SEGMENT_TITLES = {
    "practical_service": "General Practical Readers",
    "value_conscious": "Cost & Value-Conscious Readers",
    "policy_engaged": "Policy & Politically Engaged Readers",
    "industry_trade": "Industry & Trade Professionals",
    "advocacy_equity": "Advocacy & Equity-Focused Readers",
}
SEGMENT_DEFS = {
    "practical_service": "Operational/how-to service journalism: deadlines, logistics, plain facts a reader needs to act.",
    "value_conscious": "Framed around cost, savings, or comparing options by price/value.",
    "policy_engaged": "Framed around politics, legislation, regulation, or institutional debate.",
    "industry_trade": "Written for professionals/practitioners in the relevant industry, not consumers.",
    "advocacy_equity": "Framed around access, fairness, or a disadvantaged group's stake in the outcome.",
}

BATCH = 60
CONCURRENCY = 3

_SEGMENT_RULES = (
    ("policy_engaged", re.compile(r"congress|senat|legislat|regulat|administration|policy|lawmaker|republican|democrat", re.I)),
    ("industry_trade", re.compile(r"industry|trade|professional|practitioner|accountant|broker|association", re.I)),
    ("advocacy_equity", re.compile(r"low.income|equity|access|underserved|eligib|disadvantag|advocacy", re.I)),
    ("value_conscious", re.compile(r"\bfree\b|cost|price|save|discount|cheap|afford", re.I)),
    ("practical_service", re.compile(r"deadline|how to|guide|step.by.step|file by|due date", re.I)),
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
    for key, rx in _SEGMENT_RULES:
        if rx.search(text):
            return {"audience_segment": key}
    return {"audience_segment": "practical_service"}


async def _label_batch(briefs: list[dict], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "segments": SEGMENT_DEFS,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "audience_segment": "|".join(SEGMENT_KEYS) + " — the single best-fit reader segment this article's angle targets",
        }]},
    }
    system = (
        "You classify news/editorial articles by which reader segment their angle actually targets, for a PR "
        "research dashboard. Judge from the text only — pick exactly one segment per article, the closest fit even "
        "when imperfect. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Classify these articles.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=180,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        seg = str(it.get("audience_segment") or "").lower()
        out[str(it["id"])] = {"audience_segment": seg if seg in SEGMENT_KEYS else "practical_service"}
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
            logger.warning("PR research label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        by_id.setdefault(b["id"], fallback_label(by_article[b["id"]]))
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels for every article. Never raises."""
    labels = await label_articles(articles)
    return {"labels": labels}


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {"audience_segment": "practical_service"}
