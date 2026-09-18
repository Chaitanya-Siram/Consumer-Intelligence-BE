"""Perception Analysis storyboard — Landscape Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-perception-analysis.md
Screen:   Consumer-Intelligence-FE/src/screens/PerceptionAnalysisScreen.jsx

Every number is computed here from the session's tagged articles plus two
classification maps produced beforehand by perception_classify.prepare()
(raw theme -> perception key; negative post -> emotion + aspect). Prose
fields are left empty for narrative.py. `evidence` carries real samples the
narrative grounds itself in; the screen never renders it.

Emotion mix without an emotion tag (contract §5.4): Positive -> trust,
Neutral -> neutral, Negative -> fear|anger from the classifier.
"""

from collections import Counter

from .. import aggregate, brand_media, cohorts, quotes, timeseries
from ..perception_classify import (
    ASPECT_KEYS,
    ASPECT_TITLES,
    PERCEPTION_KEYS,
    PERCEPTION_TITLES,
    article_id,
    perception_key,
    prepare,  # re-exported: builder calls perception.prepare() before build_storyboard()
)

__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

LENS_KEY = "perception_analysis"

TABS = [
    {"id": "t1", "label": "General Perception"},
    {"id": "t2", "label": "Sentiment Drivers"},
    {"id": "t3", "label": "Emotional Outlook"},
]
BRAND_CHIP_SHARE = 0.10      # brand named in >= 10% of a card's posts -> chip
DRIVERS_PER_POLE = 3
MAX_QUOTES = 3
SAMPLE_PER_SECTION = 6       # evidence posts per theme / driver for the narrative
MIX_ORDER = (("Trust / Appreciation", "pos"), ("Neutral / Informational", "neu"), ("Anxiety / Fear", "warn"), ("Frustration / Anger", "neg"))


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _brand_chips(rows: list[dict], known: list[str]) -> list[str]:
    if not rows:
        return []
    tally: Counter = Counter()
    for a in rows:
        for b in aggregate.brands_in(a, known):
            tally[b] += 1
    floor = max(1, round(len(rows) * BRAND_CHIP_SHARE))
    return [b for b, n in tally.most_common(6) if n >= floor]


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    theme_map: dict[str, str] = (prepared.get("themes") or {}).get("map") or {}
    neg_labels: dict[str, dict] = (prepared.get("negative") or {}).get("by_id") or {}

    total = len(articles)
    rated = cohorts.rated(articles)
    window = timeseries.window_label(articles)

    # ── tab 1: perception themes ──────────────────────────────────────────
    by_key: dict[str, list[dict]] = {k: [] for k in PERCEPTION_KEYS}
    for a in articles:
        k = perception_key(a, theme_map)
        if k in by_key:
            by_key[k].append(a)
    classified = sum(len(v) for v in by_key.values())
    theme_pct = cohorts.pct_rows({k: len(v) for k, v in by_key.items()}, order=list(PERCEPTION_KEYS)) if classified else []
    pct_of = {r["name"]: r["pct"] for r in theme_pct}
    themes = []
    for k in PERCEPTION_KEYS:
        card = {"key": k, "title": PERCEPTION_TITLES[k], "pct": pct_of.get(k, 0), "count": len(by_key[k]), "text": ""}
        chips = _brand_chips(by_key[k], known)
        if chips:
            card["brands"] = chips
        themes.append(card)

    recommend_tally: Counter = Counter()
    for a in by_key["brands"] or articles:
        for b in aggregate.brands_in(a, known):
            recommend_tally[b] += 1
    most_recommended = recommend_tally.most_common(1)[0][0] if recommend_tally else (brand or "—")

    split = cohorts.sentiment_split(articles)
    pct_tone = {s["tone"]: s["pct"] for s in split}
    perception = {
        "banner": {
            "eyebrow": "Perception Analysis",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(sum(1 for t in themes if t["count"])), "label": "Perception themes"},
                {"value": f"{total:,}", "label": "Posts analysed"},
                {"value": most_recommended, "label": "Most-recommended brand"},
                {"value": f"{pct_tone.get('pos', 0)}%", "label": "Positive sentiment"},
            ],
        },
        "note": "",
        "summary": "",
        "keywords": [],
        "themes": themes,
        "classified": classified,
    }

    # ── tab 2: sentiment split and drivers ────────────────────────────────
    groups = []
    driver_evidence: dict[str, list] = {}
    for label, tone in (("Positive", "pos"), ("Neutral", "neu"), ("Negative", "neg")):
        rows = [a for a in rated if a["sentiment"] == label]
        pct = pct_tone.get(tone, 0)
        top = [(t, n) for t, n in aggregate.top_n(aggregate.count_by(rows, "theme", skip_junk=True), DRIVERS_PER_POLE) if n >= 1]
        drivers = []
        for t, n in top:
            drows = [a for a in rows if str(a.get("theme") or "").strip() == t]
            d = {"title": t, "text": "", "raw_theme": t, "count": n}
            chips = _brand_chips(drows, known)
            if chips:
                d["brands"] = chips
            drivers.append(d)
            driver_evidence[f"{tone}:{t}"] = [_brief(a) for a in drows[:SAMPLE_PER_SECTION]]
        if rows:
            groups.append({"tone": tone, "label": label, "pct": pct, "count": len(rows), "drivers": drivers})

    quote_rows = []
    for prefer in ("Positive", "Negative", "Neutral"):
        pool = [a for a in rated if a["sentiment"] == prefer]
        q = quotes.one(pool, needles=[brand] if brand else None)
        if q:
            q["tone"] = cohorts.TONE[prefer]
            quote_rows.append(q)
    net = cohorts.net_sentiment(articles)
    sentiment = {
        "banner": {
            "eyebrow": "Sentiment Drivers",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{pct_tone.get('pos', 0)}%", "label": "Positive"},
                {"value": f"{pct_tone.get('neg', 0)}%", "label": "Negative"},
                {"value": f"{pct_tone.get('neu', 0)}%", "label": "Neutral"},
                {"value": (f"{net:+.0f}" if net is not None else "—"), "label": "Net sentiment"},
            ],
        },
        "note": "",
        "split": [{"name": s["name"], "pct": s["pct"], "tone": s["tone"]} for s in split],
        "groups": groups,
        "quotes": quote_rows[:MAX_QUOTES],
    }

    # ── tab 3: emotion ────────────────────────────────────────────────────
    emo_counts = {name: 0 for name, _ in MIX_ORDER}
    aspect_rows: dict[str, list[dict]] = {k: [] for k in ASPECT_KEYS}
    for a in rated:
        s = a["sentiment"]
        if s == "Positive":
            emo_counts["Trust / Appreciation"] += 1
        elif s == "Neutral":
            emo_counts["Neutral / Informational"] += 1
        else:
            lab = neg_labels.get(article_id(a), {"emotion": "anger", "aspect": "none"})
            emo_counts["Anxiety / Fear" if lab["emotion"] == "fear" else "Frustration / Anger"] += 1
            if lab["aspect"] in aspect_rows:
                aspect_rows[lab["aspect"]].append(a)
    mix_rows = cohorts.pct_rows(emo_counts, order=[n for n, _ in MIX_ORDER]) if rated else []
    tone_of = dict(MIX_ORDER)
    mix = [{"name": r["name"], "pct": r["pct"], "tone": tone_of[r["name"]], "count": r["count"]} for r in mix_rows]
    neg_emotion_pct = round(sum(r["pct"] for r in mix if r["tone"] in ("warn", "neg"))) if mix else 0
    aspects = [
        {"key": k, "title": ASPECT_TITLES[k], "pct": round(len(aspect_rows[k]) * 100 / len(rated)) if rated else 0, "count": len(aspect_rows[k]), "text": ""}
        for k in ASPECT_KEYS
    ]
    top_aspect = max(aspects, key=lambda x: x["count"]) if any(x["count"] for x in aspects) else None
    pos_mix = [r for r in mix if r["tone"] in ("pos", "neu")]
    top_pos = max(pos_mix, key=lambda r: r["pct"])["name"].split(" / ")[0] if pos_mix else "—"
    emotion = {
        "banner": {
            "eyebrow": "Emotional Outlook",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(sum(1 for x in aspects if x["count"])), "label": "Negative aspects"},
                {"value": f"{neg_emotion_pct}%", "label": "Posts with negative emotion"},
                {"value": top_aspect["title"].split(" ")[0] if top_aspect else "None", "label": "Top concern"},
                {"value": top_pos, "label": "Top positive emotion"},
            ],
        },
        "note": "",
        "summary": "",
        "mix": mix,
        "lead": "",
        "aspect_label": "Negative aspects",
        "aspect_tags": ["Unhappy", "Fearful", "Stressed", "Anxious"],
        "aspects": aspects,
    }

    # Brands named anywhere in the payload -> logos
    named = {brand, *known}
    for t in themes:
        named.update(t.get("brands", []))
    for g in groups:
        for d in g["drivers"]:
            named.update(d.get("brands", []))
    named.discard("")

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "rated_mentions": len(rated),
            "logos": brand_media.brand_logos(sorted(named), articles),
            "classification": {
                "themes": (prepared.get("themes") or {}).get("method"),
                "negative": (prepared.get("negative") or {}).get("method"),
                "theme_map": {k: v for k, v in theme_map.items() if v != "none"},
                "unclassified": total - classified,
            },
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Perception Analysis · Landscape Analysis", f"Computed from {total:,} tagged posts"],
        "perception": perception,
        "sentiment": sentiment,
        "emotion": emotion,
        "evidence": {
            "themes": {k: [_brief(a) for a in by_key[k][:SAMPLE_PER_SECTION]] for k in PERCEPTION_KEYS if by_key[k]},
            "drivers": driver_evidence,
            "aspects": {k: [_brief(a) for a in aspect_rows[k][:SAMPLE_PER_SECTION]] for k in ASPECT_KEYS if aspect_rows[k]},
            "negative_reasons": [str(a.get("xai_sentiment_reason") or "")[:160] for a in rated if a["sentiment"] == "Negative"][:8],
        },
    }
