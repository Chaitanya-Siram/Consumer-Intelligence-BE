"""Brand Perception storyboard — Brand Intelligence Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-brand-perception.md
Screen:   Consumer-Intelligence-FE/src/screens/BrandPerceptionScreen.jsx

Popularity shares reuse brands.mention_counts(), the same tally behind
brand_competitive_intel's share of voice, so both lenses agree. Products,
switching intent and drivers come from brand_perception_classify.prepare().
Prose is left empty for narrative.py; `evidence` carries real samples.
"""

from collections import Counter

from .. import aggregate, brand_media, cohorts, quotes, timeseries
from ..brand_perception_classify import (
    DRIVER_KEYS,
    DRIVER_SHORT,
    has_award,
    label_of,
    normalise_product,
    prepare,  # re-exported for builder._HAS_PREPARE
    product_brand,
)
from . import brands as brand_metrics

LENS_KEY = "brand_perception"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "t1", "label": "Brand Perception"},
    {"id": "t2", "label": "Popularity"},
    {"id": "t3", "label": "Switchover Intent"},
]
MIN_PRODUCTS, MAX_PRODUCTS = 4, 6
MAX_POSTS = 8  # "What people say": the FE shows the first few, the rest sit behind a click
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    # ── popularity (tabs 1 and 2) — same tally as brand_competitive_intel ──
    brand_tagged = [a for a in articles if aggregate.brands_in(a, known)]
    tally = brand_metrics.mention_counts(brand_tagged, known)
    denom = len(brand_tagged) or 1
    popularity = []
    for name, n in aggregate.top_n(tally, len(tally)):
        row = {"name": name, "pct": round(n * 100 / denom), "count": n}
        if brand and name.lower() == brand.lower():
            row["is_brand"] = True
        popularity.append(row)
    by_brand_rows = {name: [a for a in brand_tagged if name in aggregate.brands_in(a, known)] for name in tally}
    brand_share = next((r["pct"] for r in popularity if r.get("is_brand")), 0)

    # ── products (tab 1) ──────────────────────────────────────────────────
    prod_rows: dict[str, list[dict]] = {}
    prod_display: dict[str, Counter] = {}
    for a in articles:
        name = label_of(a, prepared).get("product") or ""
        key = normalise_product(name)
        if not key or not product_brand(name, known):
            continue
        prod_rows.setdefault(key, []).append(a)
        prod_display.setdefault(key, Counter())[name.strip()] += 1
    ranked = sorted(prod_rows, key=lambda k: (-len(prod_rows[k]), k))[:MAX_PRODUCTS]
    products = []
    for key in ranked:
        rows = prod_rows[key]
        name = prod_display[key].most_common(1)[0][0]
        award_rows = [a for a in rows if has_award(a)]
        card = {"brand": product_brand(name, known), "name": name, "count": len(rows), "tags": [], "text": ""}
        if award_rows:
            card["award_posts"] = len(award_rows)
        q = quotes.one(rows, needles=[name.split()[-1]], prefer="Positive")
        if q:
            card["quote"] = {k: q[k] for k in ("text", "source", "url", "platform", "author", "date")}
        products.append(card)
    if len(products) < MIN_PRODUCTS:
        products_note = f"{len(products)} product(s) named often enough to card; contract asks for {MIN_PRODUCTS}-{MAX_PRODUCTS}"
    else:
        products_note = ""
    award_any = [a for a in brand_tagged if has_award(a) and brand and brand in aggregate.brands_in(a, known)]

    stats1 = [
        {"value": f"{brand_share}%", "label": f"{brand} share of voice" if brand else "Brand share of voice"},
        {"value": str(len(tally)), "label": "Brands tracked"},
        {"value": str(len(products)), "label": "Products discussed"},
    ]
    if award_any:
        stats1.append({"value": str(len(award_any)), "label": f"Posts citing {brand} in rankings"})
    perception = {
        "banner": {"eyebrow": "Brand Perception", "headline": "", "sub": "", "stats": stats1},
        "note": "",
        "summary": "",
        "keywords": [],
        "popularity": [{k: v for k, v in r.items() if k != "count"} for r in popularity],
        "products": products,
    }

    # ── popularity tab ────────────────────────────────────────────────────
    popularity_tab = {
        "banner": {"eyebrow": "Popularity", "headline": "", "sub": "", "stats": [{"value": f"{r['pct']}%", "label": r["name"]} for r in popularity[:6]]},
        "note": "",
        "lead": "",
        "brands": [{**{k: v for k, v in r.items() if k != "count"}, "points": []} for r in popularity],
    }

    # ── switching (tab 3) ─────────────────────────────────────────────────
    switching_rows = [a for a in articles if label_of(a, prepared).get("switching")]
    driver_rows: dict[str, list[dict]] = {k: [] for k in DRIVER_KEYS}
    for a in switching_rows:
        d = label_of(a, prepared).get("driver")
        if d in driver_rows:
            driver_rows[d].append(a)
    sw_total = len(switching_rows) or 1
    reasons = [
        {"key": k, "short": DRIVER_SHORT[k], "title": DRIVER_SHORT[k], "pct": round(len(driver_rows[k]) * 100 / sw_total), "count": len(driver_rows[k]), "text": ""}
        for k in DRIVER_KEYS if driver_rows[k]
    ]
    reasons.sort(key=lambda r: (-r["count"], r["key"]))
    top = reasons[0] if reasons else None
    pct_of = {r["key"]: r["pct"] for r in reasons}
    posts = []
    for a in switching_rows[: MAX_POSTS * 4]:
        q = quotes.one([a], needles=list(aggregate.brands_in(a, known)))
        if q and str(a.get("title") or "").strip():
            # The whole post record — link, platform, author, date — so the FE can
            # draw the real post (the builder adds embed/screenshot/preview) and
            # link out to it, not just its text.
            posts.append({**q, "title": str(a.get("title")).strip()[:140]})
        if len(posts) >= MAX_POSTS:
            break
    switching = {
        "banner": {
            "eyebrow": "Switchover Intent",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{top['pct']}%" if top else "—", "label": f"Switch for {top['short'].lower()}" if top else "Top driver"},
                {"value": f"{pct_of.get('transition', 0)}%", "label": "Entry-level to premium"},
                {"value": str(len(reasons)), "label": "Switch drivers"},
                {"value": f"{pct_of.get('multiple', 0)}%", "label": "Hold multiple brands"},
            ],
        },
        "note": "",
        "reasons": reasons,
        "posts": posts,
        "switching_posts": len(switching_rows),
    }

    named = {brand, *tally.keys(), *[p["brand"] for p in products if p.get("brand")]}
    named.discard("")
    labels_meta = prepared.get("labels") or {}
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "brand_tagged": len(brand_tagged),
            "logos": brand_media.brand_logos(sorted(named), articles),
            "method": {
                "popularity": (
                    "brands.mention_counts (the tally behind brand_competitive_intel share of voice) ÷ brand-tagged posts; "
                    "a post naming several brands counts for each, so shares need not sum to 100 and differ from share of voice, "
                    "which divides by total mentions"
                ),
                "switching": "posts the classifier flagged as switching/upgrading/holding several brands; driver pct = share of those posts",
                "products": "LLM-extracted brand-qualified product names (tagger `product mentions` offered as a hint), top by post count",
            },
            "classification": {
                "labels": labels_meta.get("method"),
                "products_found": {prod_display[k].most_common(1)[0][0]: len(prod_rows[k]) for k in sorted(prod_rows, key=lambda k: -len(prod_rows[k]))[:15]},
                "switching_posts": len(switching_rows),
                "driver_counts": {k: len(v) for k, v in driver_rows.items()},
                "note": products_note or None,
            },
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Brand Perception · Brand Intelligence", f"Computed from {total:,} tagged posts"],
        "perception": perception,
        "popularity": popularity_tab,
        "switching": switching,
        "evidence": {
            "brands": {name: [_brief(a) for a in rows[:SAMPLE]] for name, rows in by_brand_rows.items()},
            "products": {p["name"]: [_brief(a) for a in prod_rows[normalise_product(p["name"])][:SAMPLE]] for p in products},
            "product_awards": {p["name"]: [_brief(a) for a in prod_rows[normalise_product(p["name"])] if has_award(a)][:3] for p in products if p.get("award_posts")},
            "drivers": {r["key"]: [_brief(a) for a in driver_rows[r["key"]][:SAMPLE]] for r in reasons},
            "switching_sample": [_brief(a) for a in switching_rows[:SAMPLE]],
        },
    }
