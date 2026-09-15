"""Market Intelligence storyboard — news-article adaptation.

Simplified port of ConsumerIntelligence_PR/backend/app/charts/storyboard/market_intel.py.
Dropped: extraction agent (Signals), market_taxonomy keyword classifiers,
launches/campaigns/events (need social hashtags + extraction). Kept every
section computable from tagged news fields: theme/subtheme, sentiment, reach,
section/domain, date, brand mentions, region/country.

Regional rows carry `iso` + `flag` (Iconify ids) so the FE renders a flag next
to every country label.
"""

import calendar
from collections import Counter, defaultdict
from datetime import datetime

from .. import aggregate
from .. import brand_media
from . import brands as brand_metrics

LENS_KEY = "market_intelligence"

TOP_BRANDS = 7
TOP_PLATFORMS = 5
TOP_THEMES = 10
TOP_REGIONS = 8
TOP_REGION_BRANDS = 6
SNIPPET = 200
MIN_MONTH_SHARE_OF_PEAK = 0.05
PARTIAL_MONTH_CUTOFF_DAY = 25


def _pct(part: float, whole: float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _month(article: dict) -> str | None:
    day = aggregate.parse_day(article.get("date"))
    return day[:7] if day else None


def _month_label(ym: str) -> str:
    return datetime.strptime(ym, "%Y-%m").strftime("%b-%y")


def _range_label(months: list[str]) -> str:
    if not months:
        return ""
    first = datetime.strptime(months[0], "%Y-%m")
    last = datetime.strptime(months[-1], "%Y-%m")
    if first == last:
        return first.strftime("%b %Y")
    if first.year == last.year:
        return f"{first.strftime('%b')}–{last.strftime('%b %Y')}"
    return f"{first.strftime('%b %Y')}–{last.strftime('%b %Y')}"


def _region(article: dict) -> str:
    value = article.get("region") or article.get("country")
    if not value:
        countries = article.get("countries") or []
        if isinstance(countries, str):
            countries = [countries]
        value = countries[0] if countries else ""
    return str(value).strip()


def _region_meta(region: str) -> dict:
    iso = brand_media.region_iso(region)
    return {"iso": iso, "flag": brand_media.flag_icons(iso)}


def _subtheme(article: dict) -> str | None:
    for field in ("subtheme", "theme"):
        value = article.get(field)
        if value and not aggregate.is_junk(value):
            return str(value).strip()
    return None


def _snippet(article: dict) -> str:
    text = str(article.get("title") or article.get("content") or "").strip()
    return text[: SNIPPET - 1].rstrip() + "…" if len(text) > SNIPPET else text


def _sentiment_split(articles: list[dict]) -> list[dict]:
    rated = [a for a in articles if a.get("sentiment")]
    total = len(rated)
    return [
        {"name": label, "tone": tone, "value": _pct(sum(1 for a in rated if a["sentiment"] == label), total)}
        for label, tone in (("Positive", "pos"), ("Neutral", "neu"), ("Negative", "neg"))
    ]


def _positive_text(articles: list[dict]) -> str:
    rated = [a for a in articles if a.get("sentiment")]
    if not rated:
        return "no sentiment-rated articles"
    pos = sum(1 for a in rated if a["sentiment"] == "Positive")
    return f"{_pct(pos, len(rated))}% of {len(rated)} rated articles positive"


def _shares(counts: Counter, limit: int, *, others: bool = False) -> list[dict]:
    total = sum(counts.values())
    top = aggregate.top_n(dict(counts), limit)
    rows = [{"name": n, "value": v, "pct": _pct(v, total)} for n, v in top]
    if others:
        rest = total - sum(v for _, v in top)
        if rest > 0:
            rows.append({"name": "Others", "value": rest, "pct": _pct(rest, total)})
    return rows


def _brand_counts(articles: list[dict], known: list[str]) -> Counter:
    counts: Counter = Counter()
    for a in articles:
        for b in aggregate.brands_in(a, known):
            counts[b] += 1
    return counts


def _top_subthemes(articles: list[dict], n: int = 3) -> list[tuple[str, int]]:
    counts: Counter = Counter()
    for a in articles:
        s = _subtheme(a)
        if s:
            counts[s] += 1
    return aggregate.top_n(dict(counts), n)


def _by_reach(articles: list[dict]) -> list[dict]:
    return sorted(articles, key=aggregate.reach, reverse=True)


def _substantial(by_month: dict[str, int]) -> list[str]:
    peak = max(by_month.values(), default=0)
    floor = max(3, peak * MIN_MONTH_SHARE_OF_PEAK)
    return sorted(m for m, n in by_month.items() if n >= floor) or sorted(by_month)


# ── periods ───────────────────────────────────────────────────────────────


def _periods(articles: list[dict]) -> tuple[list[dict], list[dict], str, str]:
    """Split at the volume midpoint into (a, b, label_a, label_b)."""
    by_month: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        m = _month(a)
        if m:
            by_month[m].append(a)
    months = sorted(by_month)
    if len(months) < 2:
        return [], [a for m in months for a in by_month[m]], "", _range_label(months)
    total = sum(len(v) for v in by_month.values())
    running, cut = 0, 1
    for i, m in enumerate(months[:-1], start=1):
        running += len(by_month[m])
        cut = i
        if running >= total / 2:
            break
    first, second = months[:cut], months[cut:]
    return (
        [a for m in first for a in by_month[m]],
        [a for m in second for a in by_month[m]],
        _range_label(first),
        _range_label(second),
    )


# ── lens: channel impact (brand × outlet section) ─────────────────────────


MIN_CELL_POSTS = 5


def _channel_cells(posts, known, brands, platforms) -> dict:
    """Brand × platform volume for one window: rows, leaders, sov, by_platform."""
    cells = {b: Counter() for b in brands}
    for a in posts:
        p = aggregate.source_platform(a)
        if p not in platforms:
            continue
        for b in aggregate.brands_in(a, known):
            if b in cells:
                cells[b][p] += 1
    rows = [
        {"brand": b, **{p: cells[b][p] for p in platforms}, "total": sum(cells[b].values())}
        for b in brands
    ]
    total = sum(r["total"] for r in rows)
    leaders = {}
    for p in platforms:
        best = max(rows, key=lambda r: r[p], default=None)
        if best and best[p]:
            leaders[p] = {"brand": best["brand"], "posts": best[p]}
    return {
        "rows": rows,
        "leaders": leaders,
        "total": total,
        "sov": {r["brand"]: _pct(r["total"], total) for r in rows},
        "by_platform": {p: sum(r[p] for r in rows) for p in platforms},
    }


def _rate_change(before: int, after: int) -> float | None:
    return round((after - before) * 100 / before, 1) if before else None


def _channel_impact(articles, a, b, label_a, label_b, known, brands, platforms) -> dict:
    """Pages 3-6 of the reference report, computed for news data: brand × platform
    volume per comparison window, per-cell change, the takeaway chips, and the
    skeleton Lens1 dereferences without optional chaining. No extraction agent
    here, so content-mix shifts and events pillars are empty rather than invented."""
    overall = _channel_cells(articles, known, brands, platforms)

    windows = []
    for label, subset in ((label_a, a), (label_b, b)):
        if not subset:
            continue
        cells = _channel_cells(subset, known, brands, platforms)
        windows.append(
            {
                "label": label,
                "n": len(subset),
                "total": cells["total"],
                "rows": cells["rows"],
                "leaders": cells["leaders"],
                "sov": cells["sov"],
                "by_platform": cells["by_platform"],
                "posts": subset,  # stripped before return
            }
        )

    two = len(windows) == 2
    wa, wb = (windows[0], windows[1]) if two else (None, windows[0] if windows else None)
    change = {"basis": "count", "total_pct": None, "by_platform": {}, "by_brand": {}}
    if two:
        change["total_pct"] = _rate_change(wa["total"], wb["total"])
        change["by_platform"] = {p: _rate_change(wa["by_platform"][p], wb["by_platform"][p]) for p in platforms}
        totals_b = {r["brand"]: r["total"] for r in wb["rows"]}
        change["by_brand"] = {r["brand"]: _rate_change(r["total"], totals_b.get(r["brand"], 0)) for r in wa["rows"]}

    # Comparative table: one cell per brand × platform. Content mix needs the
    # extraction agent, so `mix_*`/`shifts` stay empty and the FE falls back to
    # the plain post count.
    def cell_posts(window, brand, platform):
        if not window:
            return []
        return [x for x in window["posts"] if aggregate.source_platform(x) == platform and brand in aggregate.brands_in(x, known)]

    comparative_rows = []
    for brand in brands:
        cells = {}
        for platform in platforms:
            pa = cell_posts(wa, brand, platform) if two else []
            pb = cell_posts(wb, brand, platform)
            suppressed = len(pb) < MIN_CELL_POSTS or (two and len(pa) < MIN_CELL_POSTS)
            cells[platform] = {
                "posts_a": len(pa) if two else None,
                "posts_b": len(pb),
                "change_pct": _rate_change(len(pa), len(pb)) if two and not suppressed else None,
                "suppressed": suppressed,
                "typed_a": 0,
                "typed_b": 0,
                "mix_a": [],
                "mix_b": [],
                "shifts": [],
                "interactions_per_post": round(sum(aggregate.engagement(x) for x in pb) / len(pb), 1) if pb else 0.0,
                "text": "",
            }
        comparative_rows.append({"brand": brand, "cells": cells})

    regions = _shares(Counter(_region(x) for x in articles if _region(x)), 3)
    facts = {
        "windows": [
            {"label": w["label"], "posts": w["n"], "brand_posts": w["total"], "share_of_voice_pct": w["sov"], "by_platform": w["by_platform"]}
            for w in windows
        ],
        "change": change,
        "brand_sov_shift_pp": (
            {r["brand"]: round(wb["sov"].get(r["brand"], 0.0) - wa["sov"].get(r["brand"], 0.0), 1) for r in wa["rows"]} if two else {}
        ),
        "leaders": {w["label"]: w["leaders"] for w in windows},
        "regions": regions,
        "creator_voice_share_pct": 0.0,
    }

    # Takeaway cards: a computed chip each; the model writes title and text.
    cards = []
    if two and change["total_pct"] is not None:
        cards.append({"topic": "volume", "chip": f"{change['total_pct']:+g}% brand posts", "title": "", "text": ""})
    if two and change["by_platform"]:
        p, v = max(((p, v) for p, v in change["by_platform"].items() if v is not None), key=lambda kv: abs(kv[1]), default=(None, None))
        if p:
            cards.append({"topic": "platforms", "chip": f"{v:+g}% {p}", "title": "", "text": ""})
    if wb and wb["leaders"]:
        lead = Counter(l["brand"] for l in wb["leaders"].values()).most_common(1)[0]
        cards.append({"topic": "brands", "chip": f"{lead[0]} leads {lead[1]} of {len(wb['leaders'])} platforms", "title": "", "text": ""})
    if regions:
        cards.append({"topic": "regions", "chip": f"{regions[0]['name']} {regions[0]['pct']}% of posts", "title": "", "text": ""})

    undated = sum(1 for x in articles if not _month(x))
    return {
        "period_mode": "midpoint",
        "platforms": platforms,
        "brands": brands,
        "rows": overall["rows"],
        "leaders": overall["leaders"],
        "sov": overall["sov"],
        "by_platform": overall["by_platform"],
        "headline": "",
        "summary": "",
        "takeaways": {"headline": "", "thesis": "", "cards": cards},
        "periods": [{k: v for k, v in w.items() if k not in ("posts", "sov", "by_platform")} for w in windows],
        "change": change,
        "comparative": {"intro": "", "rows": comparative_rows},
        "events": {"headline": "", "scope": "all posts (no named event in this dataset)", "pillars": []},
        "facts": facts,
        "provenance": {
            "articles_tagged": len(articles),
            "articles_used": len(articles) - undated,
            "undated": undated,
            "deduplicated": 0,
            "min_cell_posts": MIN_CELL_POSTS,
        },
    }


# ── lens: industry trends + trend tracking (theme categories) ────────────


def _industry_trends(articles, known) -> dict:
    counts: Counter = Counter()
    for a in articles:
        t = a.get("theme")
        if t and not aggregate.is_junk(t):
            counts[str(t).strip()] += 1
    rows = _shares(counts, TOP_THEMES)
    top = []
    for row in rows[:5]:
        subset = [a for a in articles if str(a.get("theme") or "").strip() == row["name"]]
        top.append(
            {
                **row,
                "posts": len(subset),
                "positive_text": _positive_text(subset),
                "top_subthemes": [{"name": s, "n": n} for s, n in _top_subthemes(subset, 2)],
                "top_brands": [b for b, _ in aggregate.top_n(dict(_brand_counts(subset, known)), 2)],
                "drivers": [],
            }
        )
    return {"total": sum(counts.values()), "categories": rows, "top": top, "headline": ""}


def _trend_tracking(a, b, label_a, label_b, known) -> dict:
    if not a or not b:
        return {"slides": [], "period_a": label_a, "period_b": label_b, "n_a": len(a), "n_b": len(b), "headline": ""}

    def theme_counts(subset):
        c: Counter = Counter()
        for x in subset:
            t = x.get("theme")
            if t and not aggregate.is_junk(t):
                c[str(t).strip()] += 1
        return c

    ca, cb = theme_counts(a), theme_counts(b)
    ta, tb = sum(ca.values()), sum(cb.values())
    names = [n for n, _ in aggregate.top_n(dict(ca + cb), TOP_THEMES)]
    rows = []
    for name in names:
        subset = [x for x in a + b if str(x.get("theme") or "").strip() == name]
        rows.append(
            {
                "category": name,
                "a_pct": _pct(ca[name], ta),
                "b_pct": _pct(cb[name], tb),
                "delta_pp": round(_pct(cb[name], tb) - _pct(ca[name], ta), 1),
                "top_subthemes": [s for s, _ in _top_subthemes(subset, 3)],
                "top_brands": [x for x, _ in aggregate.top_n(dict(_brand_counts(subset, known)), 2)],
                "insight": "",
            }
        )
    rows.sort(key=lambda r: -r["b_pct"])
    size = max(1, -(-len(rows) // 3))
    chunks = [rows[i : i + size] for i in range(0, len(rows), size)]
    titles = ["Leading categories", "Mid-tier categories", "Niche & emerging"]
    slides = [
        {"label": f"{i + 1}/{len(chunks)} — {titles[i] if i < len(titles) else 'More'}", "rows": chunk, "summary": ""}
        for i, chunk in enumerate(chunks)
    ]
    return {"slides": slides, "period_a": label_a, "period_b": label_b, "n_a": len(a), "n_b": len(b), "headline": ""}


# ── lens: volume trendline ────────────────────────────────────────────────


def _volume_trendline(articles) -> dict:
    by_month: Counter = Counter()
    for a in articles:
        m = _month(a)
        if m:
            by_month[m] += 1
    months = sorted(by_month)
    series = [{"month": _month_label(m), "key": m, "value": by_month[m]} for m in months]
    if not series:
        return {"total": len(articles), "months": [], "peak": None, "low": None, "growth_pct": None, "headline": "", "drivers": []}
    peak = max(series, key=lambda r: r["value"])
    solid = _substantial(dict(by_month))
    solid_series = [r for r in series if r["key"] in solid]
    last_day = max((d for d in (aggregate.parse_day(a.get("date")) for a in articles) if d), default=None)
    partial_month = None
    if last_day and int(last_day[8:10]) < PARTIAL_MONTH_CUTOFF_DAY and len(solid_series) > 2:
        if solid_series[-1]["key"] == last_day[:7]:
            partial_month = solid_series[-1]["month"]
            solid_series = solid_series[:-1]
    low = min(solid_series, key=lambda r: r["value"])
    growth = (
        _pct(solid_series[-1]["value"] - solid_series[0]["value"], solid_series[0]["value"])
        if len(solid_series) > 1
        else None
    )
    peak_articles = [a for a in articles if _month(a) == peak["key"]]
    platforms = aggregate.top_n(aggregate.count_by(peak_articles, "source_type", skip_junk=True), 1)
    return {
        "total": len(articles),
        "months": series,
        "peak": peak,
        "low": low,
        "growth_pct": growth,
        "growth_from": solid_series[0]["month"] if len(solid_series) > 1 else None,
        "growth_to": solid_series[-1]["month"] if len(solid_series) > 1 else None,
        "partial_month": partial_month,
        "peak_subthemes": [{"name": s, "n": n} for s, n in _top_subthemes(peak_articles, 3)],
        "peak_platform": {"name": platforms[0][0], "pct": _pct(platforms[0][1], len(peak_articles))} if platforms else None,
        "headline": "",
        "drivers": [],
    }


# ── lens: key themes ──────────────────────────────────────────────────────


def _key_themes(articles, a, b, label_a, label_b) -> dict:
    def shares(subset):
        c: Counter = Counter()
        for x in subset:
            s = _subtheme(x)
            if s:
                c[s] += 1
        return c, sum(c.values())

    overall, total = shares(articles)
    names = [n for n, _ in aggregate.top_n(dict(overall), TOP_THEMES)]
    ca, ta = shares(a)
    cb, tb = shares(b)
    rows = [
        {"theme": n, "value": overall[n], "pct": _pct(overall[n], total), "a_pct": _pct(ca[n], ta), "b_pct": _pct(cb[n], tb)}
        for n in names
    ]
    insights = [
        {
            "theme": r["theme"],
            "pct": r["pct"],
            "articles": r["value"],
            "positive_text": _positive_text([x for x in articles if _subtheme(x) == r["theme"]]),
            "a_pct": r["a_pct"],
            "b_pct": r["b_pct"],
            "text": "",
        }
        for r in rows[:6]
    ]
    return {"rows": rows, "insights": insights, "period_a": label_a, "period_b": label_b, "total": total, "headline": ""}


# ── lens: voice of user (top brands + regional prefs) ─────────────────────


def _regions_by_volume(articles, limit=TOP_REGIONS):
    by_region: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        r = _region(a)
        if r:
            by_region[r].append(a)
    floor = max(5, round(len(articles) * 0.01))
    ordered = sorted(by_region.items(), key=lambda kv: -len(kv[1]))
    kept = [(r, p) for r, p in ordered if len(p) >= floor][:limit]
    thin = [{"name": r, "n": len(p), **_region_meta(r)} for r, p in ordered if len(p) < floor]
    return kept, thin


def _voice_of_user(articles, known, brand_counts) -> dict:
    total_brand = sum(brand_counts.values())
    top_brands = []
    for name, count in aggregate.top_n(dict(brand_counts), 3):
        subset = [a for a in articles if name in aggregate.brands_in(a, known)]
        regions = aggregate.top_n(Counter(_region(a) for a in subset if _region(a)), 2)
        top_brands.append(
            {
                "name": name,
                "logo_url": brand_media.brandfetch_logo_url(brand_media.guess_brand_domain(name, subset)),
                "mentions": count,
                "share": _pct(count, total_brand),
                "tagline": f"{_pct(count, total_brand)}% of brand mentions",
                "positive_text": _positive_text(subset),
                "top_subthemes": [s for s, _ in _top_subthemes(subset, 2)],
                "top_regions": [{"name": r, **_region_meta(r)} for r, _ in regions],
                "bullets": [],
            }
        )
    regional_prefs = []
    kept, _ = _regions_by_volume(articles)
    for region, subset in kept:
        counts = _brand_counts(subset, known)
        if not counts:
            continue
        leader, n = aggregate.top_n(dict(counts), 1)[0]
        driver = _top_subthemes([a for a in subset if leader in aggregate.brands_in(a, known)], 1)
        regional_prefs.append(
            {
                "region": region,
                **_region_meta(region),
                "brand": leader,
                "mentions": n,
                "share": _pct(n, sum(counts.values())),
                "top_subtheme": driver[0][0] if driver else None,
                "driver": "",
            }
        )
    return {"top_brands": top_brands, "regional_prefs": regional_prefs, "headline": ""}


# ── lens: brand analysis ──────────────────────────────────────────────────


def _brand_analysis(articles, known, brand_counts) -> dict:
    share = _shares(brand_counts, 5, others=True)
    total_mentions = sum(brand_counts.values())
    details = []
    for brand in known:
        posts = [a for a in articles if brand in aggregate.brands_in(a, known)]
        if not posts:
            details.append({"name": brand, "mentions": 0, "pct": 0.0, "summary": f"{brand}: no articles in this dataset.", "platforms": []})
            continue
        platform_rows = []
        for platform, n in aggregate.top_n(Counter(aggregate.source_platform(a) for a in posts), 4):
            subset = [a for a in posts if aggregate.source_platform(a) == platform]
            quote_post = _by_reach(subset)[0] if subset else None
            platform_rows.append(
                {
                    "platform": platform,
                    "n": n,
                    "pct": _pct(n, len(posts)),
                    # Page 19's structure: one write-up per sub-theme on this
                    # platform (sub-themes stand in for the reference's post types).
                    "data": [{"type": s, "pct": _pct(cnt, n), "text": ""} for s, cnt in _top_subthemes(subset, 5)],
                    "top_subthemes": [s for s, _ in _top_subthemes(subset, 2)],
                    "quote": _snippet(quote_post) if quote_post else "",
                    "quote_url": (quote_post or {}).get("url") or "",
                    "text": "",
                }
            )
        lead = platform_rows[0] if platform_rows else None
        details.append(
            {
                "name": brand,
                "logo_url": brand_media.brandfetch_logo_url(brand_media.guess_brand_domain(brand, posts)),
                "mentions": len(posts),
                "pct": _pct(len(posts), total_mentions),
                "positive_text": _positive_text(posts),
                "sentiment": _sentiment_split(posts),
                "lead_platform": lead["platform"] if lead else None,
                "lead_platform_pct": lead["pct"] if lead else None,
                "summary": "",
                "platforms": platform_rows,
            }
        )
    return {"share": share, "brands": details, "global_paragraphs": [], "takeaways": []}


# ── lens: regional dashboards ─────────────────────────────────────────────


def _regional(articles, a, b, label_a, label_b, known) -> dict:
    regions = []
    kept, thin = _regions_by_volume(articles)
    for region, subset in kept:
        brands = _shares(_brand_counts(subset, known), TOP_REGION_BRANDS, others=True)
        themes = [{"name": s, "value": _pct(n, len(subset))} for s, n in _top_subthemes(subset, 3)]
        leader = brands[0] if brands and brands[0]["name"] != "Others" else None
        ra = [x for x in a if _region(x) == region]
        rb = [x for x in b if _region(x) == region]
        # Sub-theme mix per window (the reference's product-category trend
        # columns); `trend_right` is the whole-window mix.
        trend_types: list[dict] = []
        if ra and rb:
            ca = Counter(dict(_top_subthemes(ra, 50)))
            cb = Counter(dict(_top_subthemes(rb, 50)))
            ta, tb = sum(ca.values()), sum(cb.values())
            for name, _ in aggregate.top_n(dict(ca + cb), 6):
                trend_types.append(
                    {
                        "type": name,
                        "a_pct": _pct(ca[name], ta) if ca[name] else None,
                        "b_pct": _pct(cb[name], tb) if cb[name] else None,
                    }
                )
        trend_right = [{"type": s, "value": _pct(n, len(subset))} for s, n in _top_subthemes(subset, 6)]
        regions.append(
            {
                "name": region,
                **_region_meta(region),
                "n": len(subset),
                "share": _pct(len(subset), len(articles)),
                "sentiment": _sentiment_split(subset),
                "positive_text": _positive_text(subset),
                "channels": len({aggregate.source_platform(x) for x in subset}),
                "leader": leader,
                "period_a": {"label": label_a, "n": len(ra)},
                "period_b": {"label": label_b, "n": len(rb)},
                "summary": "",
                "key_insights": [],
                "top_themes": themes,
                "topics": [_snippet(x) for x in _by_reach(subset)[:3] if _snippet(x)],
                "trend_types": trend_types,
                "trend_right": trend_right,
                "brands": brands,
            }
        )
    return {
        "regions": regions,
        "excluded": thin,
        "min_posts": max(5, round(len(articles) * 0.01)),
        "period_a": label_a,
        "period_b": label_b,
    }


# ── assembly ──────────────────────────────────────────────────────────────


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    known = list(dict.fromkeys([b for b in [brand, *known_brands] if b]))
    days = sorted(d for d in aggregate.bucket_by_day(articles) if d != aggregate.UNKNOWN_DAY)
    window = f"{days[0]} – {days[-1]}" if days else "No dated coverage"

    a, b, label_a, label_b = _periods(articles)
    if not a:
        # Single-period collapse: one window carries everything, as period A.
        a, b, label_a, label_b = b, [], label_b, ""
    brand_counts = _brand_counts(articles, known)
    brands = [n for n, _ in aggregate.top_n(dict(brand_counts), TOP_BRANDS)]
    platform_counts = aggregate.count_by(articles, "source_type", skip_junk=True) or aggregate.count_by(
        articles, "section", skip_junk=True
    )
    platforms = [p for p, _ in aggregate.top_n(platform_counts, TOP_PLATFORMS)]
    regions = sorted({_region(x) for x in articles if _region(x)})

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [x for x in known if x != brand],
            "brands": brands,
            "platforms": platforms,
            "regions": [{"name": r, **_region_meta(r)} for r in regions],
            "total_conversations": len(articles),
            "window_label": window,
            "period_a": {"label": label_a, "n": len(a)},
            "period_b": {"label": label_b, "n": len(b)},
            "period_mode": "midpoint",
            "extraction": "none",
            "dataset_mode": brand_metrics.dataset_mode(brand_metrics.share_of_voice(articles, known)),
            "logos": brand_media.brand_logos(known, articles),
        },
        "channel_impact": _channel_impact(articles, a, b, label_a, label_b, known, brands, platforms),
        "industry_trends": _industry_trends(articles, known),
        "trend_tracking": _trend_tracking(a, b, label_a, label_b, known),
        "volume_trendline": _volume_trendline(articles),
        "key_themes": _key_themes(articles, a, b, label_a, label_b),
        "voice_of_user": _voice_of_user(articles, known, brand_counts),
        "brand_analysis": _brand_analysis(articles, known, brand_counts),
        "new_launches": {"total": 0, "categories": [], "products": {}, "headline": "", "status": "unavailable_for_news"},
        "campaigns": {"campaigns": [], "status": "unavailable_for_news"},
        "events": {"events": [], "total": 0, "status": "unavailable_for_news"},
        "regional": _regional(articles, a, b, label_a, label_b, known),
    }
