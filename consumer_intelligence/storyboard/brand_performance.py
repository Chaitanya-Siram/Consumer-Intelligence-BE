"""Brand Performance storyboard — Whitespace & Gap Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-whitespace-gap.md §4.3
Screen:   Consumer-Intelligence-FE/src/screens/BrandPerformanceScreen.jsx

Share of voice reuses brands.mention_counts (the brand_competitive_intel
tally), normalised to 100 over the brands shown. Sentiment per brand is the
same cohorts.sentiment_split the other lenses use. Digital-experience posts
come from the shared whitespace classifier. No secondary-research input, so
the `mobile` tab is omitted and `tabs` carries two entries.
"""

from .. import aggregate, brand_media, cohorts, timeseries
from ..whitespace_classify import PREPARE_KEY, label_of, prepare
from . import brands as brand_metrics

LENS_KEY = "brand_performance"
__all__ = ["LENS_KEY", "PREPARE_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "t1", "label": "Share of Voice & Sentiment"},
    {"id": "t2", "label": "Digital Experience"},
]
MAX_BRANDS = 6
MIN_DIGITAL_POSTS = 3
MIN_THEMES, MAX_THEMES = 4, 8
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    # ── tab 1: share of voice + sentiment ─────────────────────────────────
    brand_tagged = [a for a in articles if aggregate.brands_in(a, known)]
    tally = brand_metrics.mention_counts(brand_tagged, known)
    shown = [n for n, _ in aggregate.top_n(tally, MAX_BRANDS)]
    share_rows = cohorts.pct_rows({n: tally[n] for n in shown}, order=shown) if shown else []
    share = []
    for r in share_rows:
        row = {"name": r["name"], "pct": r["pct"]}
        if brand and r["name"].lower() == brand.lower():
            row["is_brand"] = True
        share.append(row)
    rows_of = {n: [a for a in brand_tagged if n in aggregate.brands_in(a, known)] for n in shown}
    sentiment = []
    for n in shown:
        split = {s["tone"]: s["pct"] for s in cohorts.sentiment_split(rows_of[n])}
        if not split:
            continue
        row = {"name": n, "pos": split.get("pos", 0), "neu": split.get("neu", 0), "neg": split.get("neg", 0)}
        if brand and n.lower() == brand.lower():
            row["is_brand"] = True
        sentiment.append(row)
    brand_row = next((r for r in sentiment if r.get("is_brand")), None)
    comp_rows = [r for r in sentiment if not r.get("is_brand")]
    best_comp = max(comp_rows, key=lambda r: r["pos"] - r["neg"]) if comp_rows else None
    brand_rank = next((i + 1 for i, r in enumerate(share) if r.get("is_brand")), None)
    voice = {
        "banner": {
            "eyebrow": "Share of Voice & Sentiment",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{next((r['pct'] for r in share if r.get('is_brand')), 0)}%", "label": f"{brand} share of voice" if brand else "Brand share of voice"},
                {"value": f"#{brand_rank}" if brand_rank else "—", "label": f"Rank of {len(share)} brands"},
                {"value": f"{brand_row['pos'] - brand_row['neg']:+d}" if brand_row else "—", "label": f"{brand} net sentiment" if brand else "Net sentiment"},
                {"value": f"{best_comp['pos'] - best_comp['neg']:+d}" if best_comp else "—", "label": f"{best_comp['name']} net sentiment" if best_comp else "Best competitor"},
            ],
        },
        "note": "",
        "share": share,
        "sentiment": sentiment,
        "callout": "",
        "best_competitor": best_comp["name"] if best_comp else None,
    }

    # ── tab 2: digital experience ─────────────────────────────────────────
    digital_brands = []
    evidence_digital: dict[str, dict] = {}
    ordered = ([brand] if brand and brand in shown else []) + [n for n in shown if n.lower() != (brand or "").lower()]
    for n in ordered:
        drows = [a for a in rows_of[n] if label_of(a, prepared)["digital_topic"]]
        if len(drows) < MIN_DIGITAL_POSTS:
            continue
        theme_counts = aggregate.count_by(drows, "theme", skip_junk=True)
        top = aggregate.top_n(theme_counts, MAX_THEMES)
        themes = [{"name": t, "pct": round(c * 100 / len(drows))} for t, c in top]
        digital_brands.append({
            "name": n,
            "is_brand": bool(brand) and n.lower() == brand.lower(),
            "posts": len(drows),
            "themes": themes,
            "working": [],
            "not_working": [],
        })
        evidence_digital[n] = {
            "positive": [_brief(a) for a in drows if a.get("sentiment") == "Positive"][:SAMPLE],
            "negative": [_brief(a) for a in drows if a.get("sentiment") == "Negative"][:SAMPLE],
            "all": [_brief(a) for a in drows[:SAMPLE]],
        }
    digital_total = sum(1 for a in articles if label_of(a, prepared)["digital_topic"])
    top_digital_theme = None
    if digital_brands:
        agg = aggregate.count_by([a for n in ordered for a in rows_of[n] if label_of(a, prepared)["digital_topic"]], "theme", skip_junk=True)
        top_digital_theme = aggregate.top_n(agg, 1)[0][0] if agg else None
    digital = {
        "banner": {
            "eyebrow": "Digital Experience",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{digital_total:,}", "label": "Digital-experience posts"},
                {"value": str(len(digital_brands)), "label": "Brands with digital coverage"},
                {"value": top_digital_theme or "—", "label": "Top digital theme"},
                {"value": f"{round(digital_total * 100 / total)}%" if total else "—", "label": "Share of all posts"},
            ],
        },
        "note": "",
        "brands": digital_brands,
    }

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos([brand, *shown], articles),
            "method": {
                "share": "brands.mention_counts (the brand_competitive_intel tally) over the top brands shown, normalised to 100",
                "sentiment": "cohorts.sentiment_split over each brand's rated posts (pos+neu+neg = 100)",
                "digital": "posts the shared classifier marked digital_topic; theme pct within that subset",
                "mobile": "omitted — no secondary-research input",
            },
            "classification": {"labels": (prepared.get("labels") or {}).get("method"), "digital_posts_by_brand": {b["name"]: b["posts"] for b in digital_brands}},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Brand Performance · Whitespace & Gap Analysis", f"Computed from {total:,} tagged posts"],
        "voice": voice,
        "digital": digital,
        "evidence": {
            "brand_posts": {n: [_brief(a) for a in rows_of[n][:6]] for n in shown},
            "digital": evidence_digital,
        },
    }
