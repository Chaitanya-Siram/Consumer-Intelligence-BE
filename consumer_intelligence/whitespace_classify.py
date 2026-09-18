"""Shared per-article classification for the Whitespace & Gap Analysis lenses
(audience_expectation, brand_messaging, brand_performance).

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-whitespace-gap.md §5.

One batched, temperature-0 LLM pass labels every relevant post with every
signal the three lenses need, so the builder runs it once per build (the
three modules share PREPARE_KEY = "whitespace"):

  attribute        service | security | trust | cs | limit | none   (expectations about the brand)
  unmet            short label of a want / gap / frustration, or ""
  digital          short label of a digital / app / online / payment complaint, or ""
  digital_topic    bool — the post is about a digital experience at all
  initiative_brand tracked brand the post says did something (campaign, offer,
                   partnership, launch, programme), or ""
  initiative       short message-theme label for that initiative, or ""
  usage            bool — the post says why or how people use the product

Free labels are then merged into a small set of themes by a second small
call (unmet ≤ 5, digital exactly 4 when possible, initiatives 3–6 per brand).

No secondary-research input exists in the project model, so nothing here
ever produces `ext: true`. Fallbacks keep the lenses honest when the LLM is
unavailable: attribute/usage/digital from keyword rules on theme + title,
initiative from title verbs, no unmet labels.
"""

import asyncio
import logging
import re

from . import aggregate
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PREPARE_KEY = "whitespace"

ATTRIBUTE_KEYS = ("service", "security", "trust", "cs", "limit")
ATTRIBUTE_NAMES = {
    "service": "Product range and features",
    "security": "Safety and protection",
    "trust": "Trust and loyalty",
    "cs": "Customer service and retail experience",
    "limit": "Price and availability limits",
}
ATTRIBUTE_DEFS = {
    "service": "What the brand offers: range, features, formats, kits, results the products deliver.",
    "security": "Safety and protection: safe on surfaces, protects the vehicle, no damage, non-toxic, reliable outcome.",
    "trust": "Trust, reputation, loyalty, recommendations, long-standing use, brand credibility.",
    "cs": "Customer service, support, returns, retail and delivery experience.",
    "limit": "Constraints: price, discounts wanted, availability, stock, sizes and quantity limits.",
}
BATCH = 60
CONCURRENCY = 3

_ATTR_RULES = (
    ("cs", re.compile(r"customer service|support|return|refund|delivery|shipping|store experience|retail", re.I)),
    ("limit", re.compile(r"price|discount|deal|cost|availability|stock|out of stock|limit|size|pack", re.I)),
    ("security", re.compile(r"safe|protect|damage|toxic|harm|secure|streak|residue", re.I)),
    ("trust", re.compile(r"trust|loyal|reputation|recommend|award|reliable|favourite|favorite", re.I)),
    ("service", re.compile(r"feature|range|kit|product|offer|format|result|performance|shine|clean", re.I)),
)
_DIGITAL_RX = re.compile(r"\bapp\b|website|online|login|checkout|e-?commerce|digital|amazon|order|notification|subscription|website|site\b", re.I)
_USAGE_RX = re.compile(r"how[- ]?to|guide|routine|tip|use|using|apply|application|detailing|maintenance|clean", re.I)
_INITIATIVE_RX = re.compile(r"launch|announce|partner|campaign|sponsor|unveil|introduc|program|collab|debut|expand|release", re.I)


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def _brief(a: dict, chars: int = 260) -> dict:
    return {
        "id": article_id(a),
        "title": str(a.get("title") or "")[:140],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "theme": str(a.get("theme") or "")[:60],
        "sentiment": a.get("sentiment"),
        "brands": sorted(aggregate.brands_in(a, a.get("_known") or [])) if a.get("_known") else None,
    }


def _clean_label(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip(" .").lower()[:48]


async def _label_batch(briefs: list[dict], brand: str, known: list[str], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "tracked_brands": known,
        "attributes": ATTRIBUTE_DEFS,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "attribute": "|".join(ATTRIBUTE_KEYS) + "|none — the expectation or judgement the post expresses about the tracked brand's offer; none when it expresses none",
            "unmet": "2-4 word label of a want, gap or frustration the post voices (e.g. 'longer lasting shine', 'clearer instructions'), or empty",
            "digital": "2-4 word label of a digital/app/online/payment complaint, or empty",
            "digital_topic": "bool — post is about an app, website, online shopping, ordering, checkout or notifications",
            "initiative_brand": "tracked brand name when the post is about something that brand did (campaign, offer, partnership, launch, programme, sponsorship), else empty",
            "initiative": "2-4 word message-theme label for that initiative (e.g. 'holiday bundle promotion', 'retail partnership'), or empty",
            "usage": "bool — post says why or how people use the product",
        }]},
    }
    system = (
        "You label consumer and news posts about a brand and its category for a whitespace-and-gap dashboard. "
        "Label each post on every field. Use empty strings and false freely: most news posts voice no unmet need "
        "and no digital complaint. Judge from the text only. Return ONLY JSON."
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
    known_low = {k.lower(): k for k in known}
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        attr = str(it.get("attribute") or "none").lower()
        ib = str(it.get("initiative_brand") or "").strip()
        ib_canon = known_low.get(ib.lower()) or next((known_low[k] for k in known_low if k in ib.lower()), "")
        out[str(it["id"])] = {
            "attribute": attr if attr in ATTRIBUTE_KEYS else "none",
            "unmet": _clean_label(it.get("unmet")),
            "digital": _clean_label(it.get("digital")),
            "digital_topic": bool(it.get("digital_topic")) or bool(_clean_label(it.get("digital"))),
            "initiative_brand": ib_canon,
            "initiative": _clean_label(it.get("initiative")) if ib_canon else "",
            "usage": bool(it.get("usage")),
        }
    return out


def fallback_label(a: dict, known: list[str]) -> dict:
    text = f"{a.get('theme') or ''} {a.get('title') or ''}"
    attr = next((k for k, rx in _ATTR_RULES if rx.search(text)), "none")
    brands = aggregate.brands_in(a, known)
    ib = next(iter(brands)) if len(brands) == 1 and _INITIATIVE_RX.search(text) else ""
    return {
        "attribute": attr,
        "unmet": "",
        "digital": "",
        "digital_topic": bool(_DIGITAL_RX.search(text)),
        "initiative_brand": ib,
        "initiative": _clean_label(a.get("theme")) if ib else "",
        "usage": bool(_USAGE_RX.search(text)),
    }


async def label_articles(articles: list[dict], *, brand: str, known: list[str]) -> dict:
    briefs = []
    for a in articles:
        if article_id(a):
            b = _brief(a)
            b["brands"] = sorted(aggregate.brands_in(a, known)) or None
            briefs.append(b)
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
            logger.warning("Whitespace label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        by_id.setdefault(b["id"], fallback_label(by_article[b["id"]], known))
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:40] or "theme"


def _title(label: str) -> str:
    return " ".join(w.capitalize() if i == 0 else w for i, w in enumerate(label.split()))


async def merge_labels(labels: dict[str, int], *, min_n: int, max_n: int, kind: str, brand: str = "") -> dict:
    """Merge free labels (label -> count) into min_n..max_n themes.

    Returns {"themes": [{"key","title","raw"}], "map": {raw: key}, "method"}.
    Fewer than min_n distinct labels -> each label is its own theme (never padded)."""
    if not labels:
        return {"themes": [], "map": {}, "method": "empty"}
    ranked = sorted(labels, key=lambda k: (-labels[k], k))[:150]
    if len(ranked) <= max_n:
        themes = [{"key": slug(l), "title": _title(l), "raw": [l]} for l in ranked]
        return _finish(themes, labels, "direct")
    payload = {
        "brand": brand or None,
        "what": kind,
        "min_themes": min_n,
        "max_themes": max_n,
        "labels": [{"label": l, "count": labels[l]} for l in ranked],
        "schema": {"themes": [{"title": "str 2-5 words, Title Case", "raw": ["labels from input"]}]},
    }
    system = (
        f"You merge near-duplicate labels describing {kind} into a small set of distinct themes for a dashboard. "
        "Every input label appears in exactly one theme. Prefer merging singletons into a broader theme over "
        "creating tiny ones. Titles are short noun phrases. Return ONLY JSON."
    )
    try:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Merge these labels.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=90,
        )
        themes, assigned = [], set()
        for it in (raw.get("themes") if isinstance(raw, dict) else None) or []:
            if not isinstance(it, dict):
                continue
            title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()[:48]
            members = [_clean_label(m) for m in (it.get("raw") or []) if _clean_label(m) in labels and _clean_label(m) not in assigned]
            if title and members:
                themes.append({"key": slug(title), "title": title, "raw": members})
                assigned.update(members)
        if not themes:
            raise ValueError("no usable themes")
        leftover = [l for l in ranked if l not in assigned]
        themes.sort(key=lambda t: -sum(labels[r] for r in t["raw"]))
        if leftover:
            themes[-1]["raw"].extend(leftover)
        if len(themes) > max_n:
            head, tail = themes[: max_n - 1], themes[max_n - 1 :]
            head.append({"key": "other", "title": "Other", "raw": [r for t in tail for r in t["raw"]]})
            themes = head
        return _finish(themes, labels, "llm")
    except Exception as exc:
        logger.warning("Label merge (%s) fell back to top labels: %s", kind, exc)
        keep = ranked[: max_n - 1]
        themes = [{"key": slug(l), "title": _title(l), "raw": [l]} for l in keep]
        rest = [l for l in ranked if l not in keep]
        if rest:
            themes.append({"key": "other", "title": "Other", "raw": rest})
        return _finish(themes, labels, "fallback")


def _finish(themes: list[dict], labels: dict[str, int], method: str) -> dict:
    seen: set[str] = set()
    for t in themes:
        base, k, i = t["key"], t["key"], 2
        while k in seen:
            k, i = f"{base}-{i}", i + 1
        t["key"] = k
        seen.add(k)
        t["count"] = sum(labels.get(r, 0) for r in t["raw"])
    themes.sort(key=lambda t: (t["key"] == "other", -t["count"], t["key"]))
    return {"themes": themes, "map": {r: t["key"] for t in themes for r in t["raw"]}, "method": method}


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels for every post plus merged theme maps. Never raises."""
    known = known_brands or ([brand] if brand else [])
    labels = await label_articles(articles, brand=brand, known=known)
    by_id = labels["by_id"]
    unmet_counts: dict[str, int] = {}
    digital_counts: dict[str, int] = {}
    init_counts: dict[str, dict[str, int]] = {}
    for lab in by_id.values():
        if lab["unmet"]:
            unmet_counts[lab["unmet"]] = unmet_counts.get(lab["unmet"], 0) + 1
        if lab["digital"]:
            digital_counts[lab["digital"]] = digital_counts.get(lab["digital"], 0) + 1
        if lab["initiative_brand"] and lab["initiative"]:
            d = init_counts.setdefault(lab["initiative_brand"], {})
            d[lab["initiative"]] = d.get(lab["initiative"], 0) + 1
    unmet, digital, *inits = await asyncio.gather(
        merge_labels(unmet_counts, min_n=4, max_n=5, kind="unmet consumer needs", brand=brand),
        merge_labels(digital_counts, min_n=4, max_n=4, kind="digital experience complaints", brand=brand),
        *(merge_labels(c, min_n=3, max_n=6, kind=f"{b} brand initiatives and messages", brand=brand) for b, c in init_counts.items()),
    )
    return {
        "labels": labels,
        "unmet": unmet,
        "digital": digital,
        "initiatives": dict(zip(init_counts.keys(), inits)),
    }


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {
        "attribute": "none", "unmet": "", "digital": "", "digital_topic": False, "initiative_brand": "", "initiative": "", "usage": False,
    }
