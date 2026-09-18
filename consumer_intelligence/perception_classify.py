"""Classification inputs for the Perception Analysis lens.

Two LLM classification passes, both temperature 0, both with deterministic
fallbacks, both returning mappings that code then counts over:

1. `classify_themes` — maps every raw tagger `theme` label to one of the six
   fixed perception keys (or "none"). One call over the label list, not one
   per article: 60 labels instead of 500 posts, cacheable and auditable. The
   deck's definitions are credit-card flavoured; the prompt restates them
   generically so they apply to any category.

2. `classify_negative` — for negative posts only (usually a handful), decides
   fear vs anger and the negative aspect (debt / literacy / stress or none)
   from title + summary + the tagger's own sentiment reasoning.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-perception-analysis.md §5.
"""

import asyncio
import logging
import re

from . import aggregate
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PERCEPTION_KEYS = ("benefits", "caution", "literacy", "brands", "parents", "concerns")
PERCEPTION_TITLES = {
    "benefits": "Perceived Benefits",
    "caution": "Cautionary Use",
    "literacy": "Building Know-How",
    "brands": "Brand Preferences",
    "parents": "Household Influence",
    "concerns": "Concerns and Advice",
}
# Generic restatement of the deck definitions (§5.2), category-neutral.
PERCEPTION_DEFINITIONS = {
    "benefits": "The product or category seen as a tool for future benefit: value delivered, results achieved, protection, opportunities it opens up.",
    "caution": "How to use it responsibly; warnings about misuse, overuse or side effects; mixed views on discipline and restraint.",
    "literacy": "Learning and know-how gained through the product: guides, how-tos, tips, education, rewards or incentives for good habits.",
    "brands": "Recommendations of specific brands or products, ease of buying or approval, head-to-head comparisons, reviews naming brands.",
    "parents": "Family or household involvement: shared use, gifting, advice from parents or relatives, decisions made for others in the home.",
    "concerns": "Costs, fees, prices, risks, complaints and the advice people give to avoid them.",
}

EMOTIONS = ("fear", "anger")
ASPECT_KEYS = ("debt", "literacy", "stress")
ASPECT_TITLES = {"debt": "Cost Concerns", "literacy": "Knowledge Gaps", "stress": "Stress and Frustration"}
ASPECT_DEFINITIONS = {
    "debt": "Money burden: cost, price, fees, wasted spend, financial risk.",
    "literacy": "A knowledge or how-to gap: confusion, misuse from not knowing, wanting guidance.",
    "stress": "Emotional strain: stress, anxiety, frustration, disappointment with the experience itself.",
}

_KEYWORD_RULES = (
    ("concerns", re.compile(r"critique|complain|issue|problem|price|cost|fee|expens|risk|recall|defect|fail|warranty", re.I)),
    ("caution", re.compile(r"caution|warn|safety|safe|careful|responsib|damage|avoid|misuse|hazard", re.I)),
    ("literacy", re.compile(r"guide|how[- ]?to|tip|tutorial|learn|educat|explain|instruction|diy|maintenance routine|advice", re.I)),
    ("brands", re.compile(r"brand|recommend|compar|review|versus|\bvs\b|rating|best|top \d|alternative|endorse|award|trust", re.I)),
    ("parents", re.compile(r"parent|family|household|gift|kids|children|spouse|dad|mom|teen", re.I)),
    ("benefits", re.compile(r"benefit|performance|protect|result|value|shine|clean|feature|integration|quality|effective|durab|launch|promotion|deal|discount|listing|offer", re.I)),
)


def keyword_theme(label: str) -> str:
    for key, rx in _KEYWORD_RULES:
        if rx.search(label or ""):
            return key
    return "none"


def _validate_theme_map(raw: dict, labels: set[str]) -> dict[str, str]:
    items = raw.get("themes") if isinstance(raw, dict) else None
    out: dict[str, str] = {}
    if isinstance(items, dict):
        items = [{"label": k, "key": v} for k, v in items.items()]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()
        key = str(it.get("key") or "").strip().lower()
        if label in labels and (key in PERCEPTION_KEYS or key == "none"):
            out[label] = key
    return out


async def classify_themes(articles: list[dict], *, brand: str = "", timeout: float = 90.0) -> dict:
    """{"map": {raw_theme: key|"none"}, "method": "llm"|"fallback"|"empty"}."""
    counts = aggregate.count_by(articles, "theme", skip_junk=True)
    if not counts:
        return {"map": {}, "method": "empty"}
    labels = sorted(counts, key=lambda k: -counts[k])[:120]
    payload = {
        "brand": brand or None,
        "perception_keys": PERCEPTION_DEFINITIONS,
        "raw_themes": [{"label": l, "count": counts[l]} for l in labels],
        "schema": {"themes": [{"label": "label from input", "key": "|".join(PERCEPTION_KEYS) + "|none"}]},
    }
    system = (
        "You classify tagging labels from news and social coverage about a brand and its category into a "
        "fixed set of consumer-perception themes. Each raw label gets exactly one key from perception_keys, "
        "or 'none' when the label is about none of them (e.g. pure market statistics). Read the definitions "
        "generically for this category; do not assume financial products. Return ONLY JSON."
    )
    try:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Classify these labels.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=timeout,
        )
        mapping = _validate_theme_map(raw, set(labels))
        if len(mapping) < max(1, len(labels) // 2):
            raise ValueError(f"LLM mapped {len(mapping)} of {len(labels)} labels")
        for l in labels:
            mapping.setdefault(l, keyword_theme(l))
        return {"map": mapping, "method": "llm"}
    except Exception as exc:
        logger.warning("Perception theme classifier fell back to keywords: %s", exc)
        return {"map": {l: keyword_theme(l) for l in labels}, "method": "fallback"}


def perception_key(article: dict, theme_map: dict[str, str]) -> str:
    label = str(article.get("theme") or "").strip()
    if not label or aggregate.is_junk(label):
        return "none"
    return theme_map.get(label) or keyword_theme(label)


def _article_brief(a: dict, chars: int = 300) -> dict:
    return {
        "id": str(a.get("id") or a.get("article_ref") or ""),
        "title": str(a.get("title") or "")[:160],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "reason": str(a.get("xai_sentiment_reason") or "")[:200],
    }


async def classify_negative(articles: list[dict], *, timeout: float = 90.0) -> dict:
    """For Negative posts: {"by_id": {id: {"emotion": fear|anger, "aspect": debt|literacy|stress|none}}, "method"}."""
    neg = [a for a in articles if a.get("sentiment") == "Negative"]
    if not neg:
        return {"by_id": {}, "method": "empty"}
    briefs = [_article_brief(a) for a in neg[:80]]
    ids = {b["id"] for b in briefs if b["id"]}
    payload = {
        "emotions": {"fear": "worry, anxiety, uncertainty, caution about what might go wrong", "anger": "frustration, annoyance, blame, disappointment about what did go wrong"},
        "aspects": ASPECT_DEFINITIONS,
        "posts": briefs,
        "schema": {"posts": [{"id": "id from input", "emotion": "fear|anger", "aspect": "debt|literacy|stress|none"}]},
    }
    system = (
        "You label the emotional register of negative consumer posts. For each post choose exactly one emotion "
        "and one negative aspect (or 'none'). Judge from the text and the tagger's reason only. Return ONLY JSON."
    )
    try:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=timeout,
        )
        out: dict[str, dict] = {}
        for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
            if not isinstance(it, dict) or str(it.get("id")) not in ids:
                continue
            emo = str(it.get("emotion") or "").lower()
            asp = str(it.get("aspect") or "none").lower()
            out[str(it["id"])] = {"emotion": emo if emo in EMOTIONS else "anger", "aspect": asp if asp in ASPECT_KEYS else "none"}
        if not out:
            raise ValueError("LLM returned no usable labels")
        for b in briefs:
            out.setdefault(b["id"], {"emotion": "anger", "aspect": "none"})
        return {"by_id": out, "method": "llm"}
    except Exception as exc:
        logger.warning("Negative-emotion classifier fell back: %s", exc)
        return {"by_id": {b["id"]: {"emotion": "anger", "aspect": "none"} for b in briefs}, "method": "fallback"}


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Everything the sync builder needs that requires an LLM. Never raises."""
    themes, negative = await asyncio.gather(classify_themes(articles, brand=brand), classify_negative(articles))
    return {"themes": themes, "negative": negative}
