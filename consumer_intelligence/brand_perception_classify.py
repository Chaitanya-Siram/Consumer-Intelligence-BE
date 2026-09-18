"""Per-article classification for the Brand Perception lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-brand-perception.md §5.

One batched, temperature-0 LLM pass labels every relevant post with:
  product   — a brand-qualified product name named in the post, or ""
              (restricted to the tracked brands; the tagger's sparse
              `product mentions` field is offered as a hint)
  switching — whether the post shows switching / upgrading / replacing /
              holding several brands
  driver    — rewards | transition | multiple | network | history | other | none

Fallbacks: product from `product mentions` + the post's single tracked brand;
switching from theme keywords or naming two or more brands; driver "other".
Everything returned is reported under meta.classification for audit; the LLM
never produces a number here.
"""

import asyncio
import logging
import re

from . import aggregate
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

DRIVER_KEYS = ("rewards", "transition", "multiple", "network", "history")
DRIVER_SHORT = {
    "rewards": "Rewards and Benefits",
    "transition": "Entry-Level to Premium",
    "multiple": "Multiple Brands",
    "network": "Major Brands and Retailers",
    "history": "Long-Term Loyalty",
}
DRIVER_DEFS = {
    "rewards": "Switching or choosing for better value: deals, discounts, bundles, bonuses, perks, more for the money.",
    "transition": "Moving up from a basic, starter or entry-level product to a premium, professional or higher-tier one.",
    "multiple": "Keeping or using several brands or products at once for flexibility, backup or different jobs.",
    "network": "Preferring big, established brands or major retailers for availability, trust and service.",
    "history": "Staying with, or returning to, a brand because of a long track record and accumulated trust.",
}
BATCH = 60
CONCURRENCY = 3
_SWITCH_RX = re.compile(r"switch|compar|alternative|versus|\bvs\b|upgrade|replace|instead of|better than|moved? (to|from)|multiple|several brands", re.I)
_AWARD_RX = re.compile(r"\b(best|ranked|ranking|award|voted|top pick|editor'?s choice|winner|#1|number one)\b", re.I)


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def _hint(a: dict) -> str:
    v = str(a.get("product mentions") or "").strip()
    return "" if v.lower() in {"", "none", "null", "n/a"} else v


def _brief(a: dict, chars: int = 260) -> dict:
    return {
        "id": article_id(a),
        "title": str(a.get("title") or "")[:140],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "theme": str(a.get("theme") or "")[:60],
        "product_hint": _hint(a)[:60],
    }


def keyword_switching(a: dict, known: list[str]) -> bool:
    text = f"{a.get('theme') or ''} {a.get('title') or ''}"
    return bool(_SWITCH_RX.search(text)) or len(aggregate.brands_in(a, known)) >= 2


def fallback_product(a: dict, known: list[str]) -> str:
    hint = _hint(a)
    if not hint:
        return ""
    brands = aggregate.brands_in(a, known)
    if len(brands) == 1:
        b = next(iter(brands))
        return hint if hint.lower().startswith(b.lower()) else f"{b} {hint}"
    return ""


async def _label_batch(briefs: list[dict], brand: str, known: list[str], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "tracked_brands": known,
        "drivers": DRIVER_DEFS,
        "posts": briefs,
        "schema": {"posts": [{"id": "id from input", "product": "brand-qualified product or line name from a tracked brand as the post names it (e.g. 'Armor All Car Wash Wipes'), or empty string", "switching": "bool", "driver": "|".join(DRIVER_KEYS) + "|other|none"}]},
    }
    system = (
        "You label consumer posts about a brand and its category. For each post: name the single most specific "
        "product of a tracked brand the post discusses (use product_hint when it fits; empty when no tracked-brand "
        "product is named); say whether the post shows switching, upgrading, replacing or holding several brands; "
        "and if it does, pick the one driver that best explains it (other when none fits, none when not switching). "
        "Judge from the text only. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=150,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        product = re.sub(r"\s+", " ", str(it.get("product") or "")).strip(" .")[:80]
        switching = bool(it.get("switching"))
        driver = str(it.get("driver") or "none").lower()
        if driver not in DRIVER_KEYS and driver != "other":
            driver = "none"
        if not switching:
            driver = "none"
        elif driver == "none":
            driver = "other"
        out[str(it["id"])] = {"product": product, "switching": switching, "driver": driver}
    return out


async def label_articles(articles: list[dict], *, brand: str, known: list[str]) -> dict:
    briefs = [_brief(a) for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, brand, known, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Brand perception label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        if b["id"] not in by_id:
            a = by_article[b["id"]]
            sw = keyword_switching(a, known)
            by_id[b["id"]] = {"product": fallback_product(a, known), "switching": sw, "driver": "other" if sw else "none"}
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


def normalise_product(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def product_brand(name: str, known: list[str]) -> str | None:
    low = name.lower()
    hits = [b for b in known if b.lower() in low]
    return max(hits, key=len) if hits else None


def has_award(a: dict) -> bool:
    return bool(_AWARD_RX.search(f"{a.get('title') or ''} {a.get('summary') or ''}"))


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels for every post. Never raises."""
    known = known_brands or ([brand] if brand else [])
    labels = await label_articles(articles, brand=brand, known=known)
    return {"labels": labels}


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {"product": "", "switching": False, "driver": "none"}
