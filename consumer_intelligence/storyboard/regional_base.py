"""Shared region computation for the three Regional Intelligence lenses.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-regional.md §4-5.

`compute_regions()` builds one record per market from the shared prepare()
result (regional_classify), carrying every agg field any of the three lenses
needs plus evidence samples. Each lens module then projects the fields it
publishes, so all three payloads share the same regions[] set and order.
"""

from collections import Counter

from .. import aggregate, brand_media, cohorts, taxonomy, timeseries
from ..regional_classify import market_of, product_type_of
from . import brands as brand_metrics

TOP_THEMES = 3
TOP_TYPES = 6
TYPE_OTHERS_MIN_PCT = 3
MAX_BRANDS = 8
MIN_BRANDS = 5
MAX_OTHERS = 8
TOPIC_BRANDS = 4
TOPIC_MIN_POSTS = 3
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _themes(rows: list[dict], tax: dict) -> list[dict]:
    """Top discussion theme groups with share of market posts and the most
    common raw theme inside each group as `sub`."""
    mapping = tax.get("map") or {}
    by_group: dict[str, Counter] = {}
    for a in rows:
        raw = str(a.get("theme") or "").strip()
        if not raw or aggregate.is_junk(raw):
            continue
        g = mapping.get(raw, taxonomy.OTHERS)
        if g == taxonomy.OTHERS:
            continue
        by_group.setdefault(g, Counter())[raw] += 1
    ranked = sorted(by_group.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:TOP_THEMES]
    n = len(rows) or 1
    return [{"name": g, "sub": c.most_common(1)[0][0], "pct": round(sum(c.values()) * 100 / n), "count": sum(c.values())} for g, c in ranked]


def _types(rows: list[dict], prepared: dict, titles: dict[str, str]) -> list[dict]:
    typed = [(product_type_of(a, prepared), a) for a in rows]
    typed = [(k, a) for k, a in typed if k]
    if not typed:
        return []
    counts = Counter(k for k, _ in typed)
    n = len(typed)
    ranked = counts.most_common()
    keep, tail = ranked[:TOP_TYPES], ranked[TOP_TYPES:]
    out = [{"name": titles.get(k, k.replace("-", " ").title()), "pct": round(c * 100 / n), "count": c} for k, c in keep]
    tail_n = sum(c for _, c in tail)
    if tail_n and round(tail_n * 100 / n) >= TYPE_OTHERS_MIN_PCT:
        out.append({"name": "Others", "pct": round(tail_n * 100 / n), "count": tail_n})
    return out


def _brands(rows: list[dict], brand: str, known: list[str]) -> tuple[list[dict], list[str], Counter]:
    tagged = [a for a in rows if aggregate.brands_in(a, known)]
    tally = brand_metrics.mention_counts(tagged, known)
    if not tally:
        return [], [], tally
    ranked = [n for n, _ in aggregate.top_n(tally, len(tally))]
    limit = MAX_BRANDS if len(ranked) > MAX_BRANDS else max(MIN_BRANDS, len(ranked))
    keep, folded = ranked[:limit], ranked[limit:]
    counts = {n: tally[n] for n in keep}
    order = list(keep)
    if folded:
        counts["Others"] = sum(tally[n] for n in folded)
        order.append("Others")
    rows_pct = cohorts.pct_rows(counts, order=order)
    out = []
    for r in rows_pct:
        row = {"name": r["name"], "pct": r["pct"]}
        if brand and r["name"].lower() == brand.lower():
            row["is_brand"] = True
        out.append(row)
    return out, folded[:MAX_OTHERS], tally


def compute_regions(articles: list[dict], *, brand: str, known: list[str], prepared: dict) -> dict:
    """{"regions": [...full records...], "periods": [label, label], "meta_extra": {...}}"""
    tax = prepared.get("taxonomy") or {}
    type_titles = {t["key"]: t["title"] for t in (prepared.get("product_types") or {}).get("themes", [])}
    markets = prepared.get("markets") or []
    by_market: dict[str, list[dict]] = {m["name"]: [] for m in markets}
    for a in articles:
        m = market_of(a, prepared)
        if m in by_market:
            by_market[m].append(a)

    # Two equal halves of the whole session window BY TIME (contract §2), shared
    # by every market: the pivot is the midpoint date, not the median post.
    start, end = timeseries.window(articles)
    pivot_day = None
    if start and end and end > start:
        pivot_day = start + (end - start) / 2
    dated_all = timeseries.dated(articles)
    early_all = [a for d, a in dated_all if pivot_day is None or d < pivot_day]
    late_all = [a for d, a in dated_all if pivot_day is not None and d >= pivot_day]
    p1 = timeseries.window_label(early_all) if early_all else "First half"
    p2 = timeseries.window_label(late_all) if late_all else "Second half"
    periods = [p1, p2]

    def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
        if pivot_day is None:
            return rows, []
        h1, h2 = [], []
        for d, a in timeseries.dated(rows):
            (h2 if d >= pivot_day else h1).append(a)
        return h1, h2

    regions = []
    all_brand_names: set[str] = {brand} if brand else set()
    for m in markets:
        rows = by_market.get(m["name"], [])
        if not rows:
            continue
        h1, h2 = split(rows)
        brands, others, tally = _brands(rows, brand, known)
        all_brand_names.update(b["name"] for b in brands if b["name"] != "Others")
        all_brand_names.update(others)
        topic_brands = [n for n, c in aggregate.top_n(tally, TOPIC_BRANDS + 2) if c >= TOPIC_MIN_POSTS and n != "Others"][:TOPIC_BRANDS]
        brand_share = next((b["pct"] for b in brands if b.get("is_brand")), 0)
        if brand and brand not in topic_brands and brand_share >= 3 and tally.get(brand, 0) >= TOPIC_MIN_POSTS:
            topic_brands = (topic_brands + [brand])[:TOPIC_BRANDS]
        split_rows = cohorts.sentiment_split(rows)
        sentiment = {s["tone"]: s["pct"] for s in split_rows}
        types_h1, types_h2 = _types(h1, prepared, type_titles), _types(h2, prepared, type_titles)
        regions.append({
            "key": m["key"],
            "name": m["name"],
            "flag": m.get("flag"),
            "mentions": len(rows),
            "headline": "",
            "summary": "",
            "insights": [],
            "sentiment": {"pos": sentiment.get("pos", 0), "neu": sentiment.get("neu", 0), "neg": sentiment.get("neg", 0)} if split_rows else None,
            "themes": _themes(rows, tax),
            "types_h1": types_h1,
            "types_h2": types_h2,
            "brands": brands,
            "others": others,
            "topics": [{"brand": b, "text": ""} for b in topic_brands],
            "sources": m.get("sources", {}),
            "_evidence": {
                "sample": [_brief(a) for a in rows[:SAMPLE]],
                "positive": [_brief(a) for a in rows if a.get("sentiment") == "Positive"][:4],
                "negative": [_brief(a) for a in rows if a.get("sentiment") == "Negative"][:4],
                "brands": {b: [_brief(a) for a in rows if b in aggregate.brands_in(a, known)][:5] for b in topic_brands},
                "h1_sample": [_brief(a) for a in h1[:4]],
                "h2_sample": [_brief(a) for a in h2[:4]],
            },
        })
    regions.sort(key=lambda r: -r["mentions"])
    return {
        "regions": regions,
        "periods": periods,
        "logos": brand_media.brand_logos(sorted(n for n in all_brand_names if n), articles),
        "meta_extra": {
            "markets_shown": [r["name"] for r in regions],
            "market_threshold": prepared.get("market_threshold"),
            "unassigned_posts": prepared.get("unassigned"),
            "market_sources": {r["name"]: r["sources"] for r in regions},
            "labels": (prepared.get("labels") or {}).get("method"),
            "product_types": (prepared.get("product_types") or {}).get("method"),
            "themes": tax.get("method"),
            "note": (
                "The tagger's `region` field is the session's configured market on every post; posts assigned by that default "
                "are counted under `sources.default` and do not prove the post came from that market."
            ),
        },
    }


def project(regions: list[dict], fields: tuple[str, ...]) -> list[dict]:
    """Copy each region record keeping the common fields plus `fields`."""
    common = ("key", "name", "flag", "mentions", "headline", "summary")
    out = []
    for r in regions:
        rec = {k: r[k] for k in common if r.get(k) is not None or k in ("headline", "summary")}
        for f in fields:
            v = r.get(f)
            if v not in (None, [], {}):
                rec[f] = v
        out.append(rec)
    return out
