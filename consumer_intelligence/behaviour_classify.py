"""Per-article classification for the User Behaviour Analysis lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-user-behaviour.md §5.

One batched, temperature-0 LLM pass labels every relevant post with:
  segment     a life-stage / audience-segment label the post itself supports
              (e.g. "new car owner", "weekend DIY enthusiast", "professional
              detailer", "parent with family car"), plus a confidence 0-1;
              empty when the post gives no clue
  multi       bool — the post shows holding, adding, switching between or
              choosing among more than one product / brand
  reason      short label: why they hold or use more than one, or ""
  rule        short label: how they choose which one to use, or ""

Then three small merges: segments -> 3-4 bands (each with a `range` descriptor),
reasons -> <= 4 clusters, rules -> <= 4 clusters. Clusters backed by fewer than
MIN_POSTS_PER_POINT posts are dropped (contract: each bullet grounded in >= 3
posts). The tagger has no age or author demographics, so the segment share is
only reported when at least SEGMENT_COVERAGE of posts get a confident band.

No number here comes from the LLM; it supplies labels that code counts.
"""

import asyncio
import logging
import re

from . import aggregate
from .narrative_client import get_narrative_client
from .whitespace_classify import merge_labels

logger = logging.getLogger(__name__)

BATCH = 60
CONCURRENCY = 3
CONFIDENCE_GATE = 0.6
SEGMENT_COVERAGE = 0.20
MIN_POSTS_PER_POINT = 3
_MULTI_RX = re.compile(r"second (product|kit|brand)|multiple|several brands|which (one|brand|product)|backup|switch|instead of|versus|\bvs\b|compar|alternat", re.I)


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip(" .").lower()[:48]


def _brief(a: dict, known: list[str], chars: int = 260) -> dict:
    return {
        "id": article_id(a),
        "title": str(a.get("title") or "")[:140],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "author": str(a.get("author") or "")[:40],
        "brands": sorted(aggregate.brands_in(a, known)) or None,
    }


async def _label_batch(briefs: list[dict], brand: str, known: list[str], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "tracked_brands": known,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "segment": "2-4 word life-stage or audience-segment label the post itself supports (who is speaking or who it is for), or empty",
            "segment_confidence": "0-1: how clearly the text supports that segment; use 0 when empty",
            "multi": "bool — post shows holding, adding, switching between or choosing among more than one product or brand",
            "reason": "2-5 word label for why they hold or use more than one (only when multi), or empty",
            "rule": "2-5 word label for how they choose which one to use (only when multi), or empty",
        }]},
    }
    system = (
        "You label consumer and news posts about a brand and its category for an audience-behaviour dashboard. "
        "Infer a segment only from explicit cues in the text (self-description, who the product is for, life "
        "situation); never guess from tone alone, and give low confidence when unsure. Most news posts have no "
        "segment cue and are not about multiple products; use empty strings and false freely. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=180,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        try:
            conf = float(it.get("segment_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        seg = _clean(it.get("segment")) if conf >= CONFIDENCE_GATE else ""
        multi = bool(it.get("multi"))
        out[str(it["id"])] = {
            "segment": seg,
            "confidence": round(conf, 2),
            "multi": multi,
            "reason": _clean(it.get("reason")) if multi else "",
            "rule": _clean(it.get("rule")) if multi else "",
        }
    return out


def fallback_label(a: dict, known: list[str]) -> dict:
    text = f"{a.get('theme') or ''} {a.get('title') or ''}"
    multi = bool(_MULTI_RX.search(text)) or len(aggregate.brands_in(a, known)) >= 2
    return {"segment": "", "confidence": 0.0, "multi": multi, "reason": "", "rule": ""}


async def label_articles(articles: list[dict], *, brand: str, known: list[str]) -> dict:
    briefs = [_brief(a, known) for a in articles if article_id(a)]
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
            logger.warning("Behaviour label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        by_id.setdefault(b["id"], fallback_label(by_article[b["id"]], known))
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


def _range_label(value) -> str:
    """Age ranges pass through ("18-24"); life-stage phrases are kept whole,
    trimmed to at most three words / 20 chars at a word boundary."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if re.search(r"\d", text):
        return text[:12]
    words = text.split()[:3]
    out = ""
    for w in words:
        if len(f"{out} {w}".strip()) > 20:
            break
        out = f"{out} {w}".strip()
    return out or words[0][:20]


async def _bands(labels: dict[str, int], *, brand: str) -> dict:
    """Merge segment labels into 3-4 bands, each with a short `range` descriptor
    (an age range when the labels carry ages, otherwise a life-stage phrase)."""
    merged = await merge_labels(labels, min_n=3, max_n=4, kind="audience life-stage segments", brand=brand)
    for t in merged.get("themes", []):
        t.setdefault("range", "")
    if merged.get("themes") and merged.get("method") in ("llm", "direct"):
        payload = {"segments": [{"key": t["key"], "title": t["title"], "examples": t["raw"][:6]} for t in merged["themes"]],
                   "schema": {"segments": [{"key": "key from input", "range": "str <= 12 chars: an age range like '18-24' when the examples imply one, else a 1-3 word life-stage descriptor", "title": "str <= 6 words, Title Case"}]}}
        try:
            raw = await asyncio.wait_for(
                get_narrative_client().complete_json(
                    [{"role": "system", "content": "You give each audience segment a short range descriptor and a clean title. Return ONLY JSON."},
                     {"role": "user", "content": f"INPUT:\n{payload}"}],
                    temperature=0.0,
                ),
                timeout=60,
            )
            by_key = {t["key"]: t for t in merged["themes"]}
            for it in (raw.get("segments") if isinstance(raw, dict) else None) or []:
                if isinstance(it, dict) and it.get("key") in by_key:
                    by_key[it["key"]]["range"] = _range_label(it.get("range"))
                    if str(it.get("title") or "").strip():
                        by_key[it["key"]]["title"] = str(it["title"]).strip()[:48]
        except Exception as exc:
            logger.warning("Segment range labelling fell back: %s", exc)
    return merged


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    known = known_brands or ([brand] if brand else [])
    labels = await label_articles(articles, brand=brand, known=known)
    seg_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    for lab in labels["by_id"].values():
        if lab["segment"]:
            seg_counts[lab["segment"]] = seg_counts.get(lab["segment"], 0) + 1
        if lab["multi"] and lab["reason"]:
            reason_counts[lab["reason"]] = reason_counts.get(lab["reason"], 0) + 1
        if lab["multi"] and lab["rule"]:
            rule_counts[lab["rule"]] = rule_counts.get(lab["rule"], 0) + 1
    segments, reasons, rules = await asyncio.gather(
        _bands(seg_counts, brand=brand),
        merge_labels(reason_counts, min_n=2, max_n=4, kind="reasons for using more than one product or brand", brand=brand),
        merge_labels(rule_counts, min_n=2, max_n=4, kind="rules for choosing which product to use", brand=brand),
    )
    return {"labels": labels, "segments": segments, "reasons": reasons, "rules": rules}


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {"segment": "", "confidence": 0.0, "multi": False, "reason": "", "rule": ""}
