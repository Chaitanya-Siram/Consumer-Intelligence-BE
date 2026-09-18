"""Audience Expectation storyboard — Whitespace & Gap Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-whitespace-gap.md §4.1
Screen:   Consumer-Intelligence-FE/src/screens/AudienceExpectationScreen.jsx

Numbers come from the tagged articles plus the shared whitespace labels
(whitespace_classify.prepare, cached per build). Prose is left empty for
narrative.py. No secondary-research input: `survey` is omitted and no bullet
carries ext: true.
"""

from collections import Counter

from .. import aggregate, brand_media, cohorts, quotes, timeseries
from ..whitespace_classify import ATTRIBUTE_KEYS, ATTRIBUTE_NAMES, PREPARE_KEY, label_of, prepare

LENS_KEY = "audience_expectation"
__all__ = ["LENS_KEY", "PREPARE_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "t1", "label": "Needs & Preferences"},
    {"id": "t2", "label": "Unmet Needs"},
    {"id": "t3", "label": "Digital Finance Gaps"},
]
MIN_PCT_SHOWN = 3
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)
    brand_rows = cohorts.brand_articles(articles, brand, known) or articles

    # ── tab 1: attributes + drivers ───────────────────────────────────────
    by_attr: dict[str, list[dict]] = {k: [] for k in ATTRIBUTE_KEYS}
    for a in brand_rows:
        k = label_of(a, prepared)["attribute"]
        if k in by_attr:
            by_attr[k].append(a)
    attr_rows = cohorts.pct_rows({k: len(v) for k, v in by_attr.items() if v}) if any(by_attr.values()) else []
    attributes = [{"key": r["name"], "name": ATTRIBUTE_NAMES[r["name"]], "pct": r["pct"], "count": r["count"], "points": []} for r in attr_rows]
    usage_rows = [a for a in articles if label_of(a, prepared)["usage"]]
    split = {s["tone"]: s["pct"] for s in cohorts.sentiment_split(brand_rows)}
    needs = {
        "banner": {
            "eyebrow": "Needs, Expectations & Preferences",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{len(brand_rows):,}", "label": f"Posts about {brand}" if brand else "Posts analysed"},
                {"value": str(len(attributes)), "label": "Expectation attributes"},
                {"value": f"{attributes[0]['pct']}%" if attributes else "—", "label": f"Top · {attributes[0]['name']}" if attributes else "Top attribute"},
                {"value": f"{split.get('pos', 0)}%", "label": "Positive posts"},
            ],
        },
        "note": "",
        "attributes": attributes,
        "drivers_title": "",
        "drivers_lead": "",
        "drivers": [],
        "usage_posts": len(usage_rows),
    }

    # ── tab 2: unmet needs ────────────────────────────────────────────────
    unmet_meta = prepared.get("unmet") or {}
    umap, utitles = unmet_meta.get("map") or {}, {t["key"]: t["title"] for t in unmet_meta.get("themes", [])}
    by_need: dict[str, list[dict]] = {}
    for a in articles:
        lab = label_of(a, prepared)["unmet"]
        k = umap.get(lab) if lab else None
        if k:
            by_need.setdefault(k, []).append(a)
    unmet_total = sum(len(v) for v in by_need.values())
    need_rows = cohorts.pct_rows({k: len(v) for k, v in by_need.items()}) if unmet_total else []
    unmet_cards = []
    for r in need_rows[:5]:
        card = {"key": r["name"], "title": utitles.get(r["name"], r["name"].replace("-", " ").title()), "count": r["count"], "text": ""}
        if r["pct"] >= MIN_PCT_SHOWN:
            card["pct"] = r["pct"]
        unmet_cards.append(card)
    unmet_rows_all = [a for v in by_need.values() for a in v]
    usplit = {s["tone"]: s["pct"] for s in cohorts.sentiment_split(unmet_rows_all)}
    unmet = {
        "banner": {
            "eyebrow": "Unmet Needs",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{unmet_total:,}", "label": "Posts voicing a need or gap"},
                {"value": str(len(unmet_cards)), "label": "Need clusters"},
                {"value": f"{need_rows[0]['pct']}%" if need_rows else "—", "label": f"Top · {unmet_cards[0]['title']}" if unmet_cards else "Top need"},
                {"value": f"{usplit.get('neg', 0)}%", "label": "Negative among them"},
            ],
        },
        "note": "",
        "needs": unmet_cards,
    }

    # ── tab 3: digital gaps ───────────────────────────────────────────────
    dmeta = prepared.get("digital") or {}
    dmap, dtitles = dmeta.get("map") or {}, {t["key"]: t["title"] for t in dmeta.get("themes", [])}
    by_pillar: dict[str, list[dict]] = {}
    for a in articles:
        lab = label_of(a, prepared)["digital"]
        k = dmap.get(lab) if lab else None
        if k:
            by_pillar.setdefault(k, []).append(a)
    pillar_rows = cohorts.pct_rows({k: len(v) for k, v in by_pillar.items()}) if by_pillar else []
    pillars = []
    for r in pillar_rows[:4]:
        card = {"key": r["name"], "title": dtitles.get(r["name"], r["name"].replace("-", " ").title()), "pct": r["pct"], "count": r["count"], "text": ""}
        q = quotes.one(by_pillar[r["name"]], needles=[brand] if brand else None, prefer="Negative")
        if q:
            card["quote"] = {"text": q["text"], "source": q["source"]}
        pillars.append(card)
    digital = {
        "banner": {
            "eyebrow": "Digital Finance Gaps",
            "headline": "",
            "sub": "",
            "stats": [{"value": f"{p['pct']}%", "label": p["title"]} for p in pillars] or [{"value": "0", "label": "Digital complaint posts"}],
        },
        "note": "",
        "pillars": pillars,
        "digital_posts": sum(len(v) for v in by_pillar.values()),
    }

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos([brand] if brand else [], articles),
            "classification": {
                "labels": (prepared.get("labels") or {}).get("method"),
                "unmet": unmet_meta.get("method"),
                "digital": dmeta.get("method"),
                "attribute_counts": {k: len(v) for k, v in by_attr.items()},
                "unmet_themes": [{"key": t["key"], "title": t["title"], "raw": t["raw"][:8]} for t in unmet_meta.get("themes", [])],
                "digital_themes": [{"key": t["key"], "title": t["title"], "raw": t["raw"][:8]} for t in dmeta.get("themes", [])],
            },
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Audience Expectation · Whitespace & Gap Analysis", f"Computed from {total:,} tagged posts"],
        "needs": needs,
        "unmet": unmet,
        "digital": digital,
        "evidence": {
            "attributes": {k: [_brief(a) for a in by_attr[k][:SAMPLE]] for k in ATTRIBUTE_KEYS if by_attr[k]},
            "usage": [_brief(a) for a in usage_rows[:12]],
            "unmet": {c["key"]: [_brief(a) for a in by_need[c["key"]][:SAMPLE]] for c in unmet_cards},
            "pillars": {p["key"]: [_brief(a) for a in by_pillar[p["key"]][:SAMPLE]] for p in pillars},
        },
    }
