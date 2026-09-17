"""Shifting Audience Priorities storyboard — Advanced Metrics Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-issues-priorities.md §5.

Every number is computed from the session's tagged articles (already
annotated with `theme_group` by taxonomy.annotate, and each group carries a
loyalty parameter bucket). Prose fields are left empty for narrative.py.

Loyalty index (documented in meta.method, stable across sessions):
    X_advocacy  = share of cohort posts whose theme group is in the advocacy bucket
    X_sentiment = (net_sentiment + 100) / 200            (0..1)
    X_usage     = share of cohort posts in the usage bucket
    X_switching = 1 - share of cohort posts in the switching bucket
    raw         = sum(W_b * X_b)                          (0..1)
    index       = 10 + 90 * raw                           (10..100)

WEIGHTS are a PLACEHOLDER pending the deck-specified values. Change them in
one place; meta.method reports whatever is in force.
"""

from collections import Counter

from .. import brand_media, cohorts, quotes, taxonomy, timeseries

LENS_KEY = "shifting_audience_priorities"

# TODO(deck): replace with the deck-specified weights. Must sum to 1.0.
WEIGHTS = {"advocacy": 0.35, "sentiment": 0.25, "usage": 0.25, "switching": 0.15}
WEIGHTS_SOURCE = "placeholder"   # -> "deck" once the approved values are in

INDEX_MIN, INDEX_MAX = 10, 100
MIN_COHORT = 3                    # buckets/cohorts below this size are not scored
CONSUMER_PLATFORMS = {"forums", "review", "reviews", "twitter", "x", "reddit", "tumblr", "blogs", "blog"}

PARAMS = [
    {"name": "Net Promoter Score (NPS)", "key": "nps"},
    {"name": "Net Sentiment Score", "key": "sentiment"},
    {"name": "Usage Frequency", "key": "usage"},
    {"name": "Switching Intent", "key": "switch"},
]
# Same 10..100 scale as the index so the meter and the gauge show one number.
BANDS = [{"to": 40, "tone": "neg"}, {"to": 70, "tone": "neu"}, {"to": 100, "tone": "pos"}]
BAND_LABEL = {"neg": "weak", "neu": "moderate", "pos": "strong"}


def band_for(score: int | None) -> dict | None:
    if score is None:
        return None
    for b in BANDS:
        if score <= b["to"]:
            return b
    return BANDS[-1]


def _bucket_shares(rows: list[dict], bucket_of: dict[str, str]) -> dict[str, float]:
    n = len(rows)
    if not n:
        return {b: 0.0 for b in taxonomy.BUCKETS}
    c = Counter(bucket_of.get(a.get(taxonomy.GROUP_FIELD) or "", "other") for a in rows)
    return {b: c.get(b, 0) / n for b in taxonomy.BUCKETS}


def loyalty_index(rows: list[dict], bucket_of: dict[str, str]) -> int | None:
    if len(rows) < MIN_COHORT:
        return None
    sh = _bucket_shares(rows, bucket_of)
    net = cohorts.net_sentiment(rows)
    x = {
        "advocacy": sh["advocacy"],
        "sentiment": ((net if net is not None else 0.0) + 100) / 200,
        "usage": sh["usage"],
        "switching": 1 - sh["switching"],
    }
    raw = sum(WEIGHTS[b] * x[b] for b in WEIGHTS)
    raw = max(0.0, min(1.0, raw))
    return int(round(INDEX_MIN + (INDEX_MAX - INDEX_MIN) * raw))


def _consumer_voice(rows: list[dict]) -> list[dict]:
    voice = [a for a in rows if cohorts.platform(a).lower() in CONSUMER_PLATFORMS]
    return voice if len(voice) >= MIN_COHORT else rows


def _nps_proxy(rows: list[dict]) -> float | None:
    return cohorts.net_sentiment(_consumer_voice(rows))


def _switching_pct(rows: list[dict], known: list[str]) -> float | None:
    if not rows:
        return None
    return round(sum(1 for a in rows if cohorts.brand_count(a, known) >= 2) * 100 / len(rows), 1)


def _usage_per_bucket(rows: list[dict], grain: str) -> float | None:
    if not rows:
        return None
    keys, groups, _ = timeseries.buckets(rows, grain)
    live = [len(groups[k]) for k in keys]
    return round(sum(live) / len(live), 1) if live else None


def _dir(delta: float | None, *, lower_is_better: bool = False) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "flat"
    good = delta < 0 if lower_is_better else delta > 0
    return "up" if good else "down"


def _tile(key: str, name: str, cur: float | None, prev: float | None, label: str, *, unit: str = "", signed: bool = True, lower_is_better: bool = False, digits: int = 1) -> dict:
    if cur is None:
        return {"key": key, "name": name, "value": "—", "unit": unit, "trend": "no data", "dir": "flat"}
    val = f"{cur:+.{digits}f}" if signed else f"{cur:.{digits}f}"
    val = val.replace(".0", "") if digits == 1 and val.endswith(".0") else val
    delta = None if prev is None else round(cur - prev, 1)
    if delta is None:
        trend = f"no prior period"
    elif abs(delta) < 0.05:
        trend = f"flat {label}"
    else:
        suffix = " pts" if unit == "%" else ""
        trend = f"{timeseries.fmt_delta(delta, digits=1)}{suffix} {label}"
    return {"key": key, "name": name, "value": val, "unit": unit, "trend": trend, "dir": _dir(delta, lower_is_better=lower_is_better)}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], taxonomy_groups: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    bucket_of = taxonomy.group_bucket(taxonomy_groups or {})
    total = len(articles)
    window = timeseries.window_label(articles)
    grain = timeseries.granularity(articles)
    cadence = "Weekly" if grain == "week" else "Monthly"

    brand_rows = cohorts.brand_articles(articles, brand, known)
    if len(brand_rows) < MIN_COHORT:
        brand_rows = articles                      # brand-only session: every post is the brand
        brand_basis = "all posts (brand not tagged per post)"
    else:
        brand_basis = f"{len(brand_rows)} posts naming {brand}"
    comp_name, comp_rows = cohorts.top_competitor(articles, brand, known)

    idx_brand = loyalty_index(brand_rows, bucket_of)
    idx_industry = loyalty_index(articles, bucket_of)
    idx_comp = loyalty_index(comp_rows, bucket_of)

    prior_rows, current_rows, period_label = timeseries.period_pair(brand_rows)
    idx_prior = loyalty_index(prior_rows, bucket_of)
    idx_current = loyalty_index(current_rows, bucket_of) or idx_brand

    # ── tab 1: loyalty ────────────────────────────────────────────────────
    def delta_str(a, b):
        return timeseries.fmt_delta(None if a is None or b is None else a - b)

    # NPS proxy is the brand cohort's advocacy; Net Sentiment is the category
    # climate over every post in the window, so the two tiles read differently.
    all_prior, all_current, _ = timeseries.period_pair(articles)
    tracking = [
        _tile("nps", "Net Promoter Score", _nps_proxy(current_rows or brand_rows), _nps_proxy(prior_rows) if prior_rows else None, period_label, digits=0),
        _tile("sentiment", "Net Sentiment", cohorts.net_sentiment(all_current or articles), cohorts.net_sentiment(all_prior) if all_prior else None, period_label),
        _tile("switch", "Switching Intent", _switching_pct(current_rows or brand_rows, known), _switching_pct(prior_rows, known) if prior_rows else None, period_label, unit="%", signed=False, lower_is_better=True),
        _tile("usage", "Usage Frequency", _usage_per_bucket(current_rows or brand_rows, grain), _usage_per_bucket(prior_rows, grain) if prior_rows else None, period_label, unit=f"/ {'wk' if grain == 'week' else 'mo'}", signed=False),
    ]
    loyalty = {
        "banner": {
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(idx_brand) if idx_brand is not None else "—", "label": "Loyalty index · current"},
                {"value": delta_str(idx_brand, idx_industry), "label": "vs industry"},
                {"value": delta_str(idx_brand, idx_comp), "label": f"vs {comp_name}" if comp_name else "vs competitor"},
                {"value": cadence, "label": "Tracking cadence"},
            ],
        },
        "index": {
            "value": idx_brand if idx_brand is not None else INDEX_MIN,
            "prior": idx_prior if idx_prior is not None else (idx_brand if idx_brand is not None else INDEX_MIN),
            "min": INDEX_MIN,
            "max": INDEX_MAX,
            "label": "Brand Loyalty Index",
            "sub": "Driven by advocacy, sentiment, usage and switching signals",
            "note": "",
        },
        "lead": "",
        "params": [{"name": p["name"], "key": p["key"], "text": ""} for p in PARAMS],
        "cadence_title": "Monthly | Quarterly Tracking" if grain == "month" else "Weekly | Monthly Tracking",
        "tracking": tracking,
    }

    # ── tab 2: trend & benchmarks ─────────────────────────────────────────
    keys, groups, _ = timeseries.buckets(brand_rows, grain)
    series = [{"x": timeseries.bucket_label(k, grain), "y": loyalty_index(groups[k], bucket_of), "key": k} for k in keys]
    live = [p for p in series if p["y"] is not None]
    points = [[p["x"], p["y"]] for p in live]
    dense = [{"x": p["x"], "y": p["y"]} for p in live]
    spikes = []
    for s in timeseries.spikes(dense):
        rows = groups[live[s["at"]]["key"]]
        top = [n for n, _ in taxonomy.group_counts(rows).most_common(3) if n != taxonomy.OTHERS]
        spikes.append({
            "at": s["at"],
            "label": f"{dense[s['at']]['x']} · {dense[s['at']]['y']}",
            "text": "",
            "ratio": s["ratio"],
            "evidence": {"posts": len(rows), "themes": top, "quote": quotes.one(rows, needles=[brand] if brand else None)},
        })
    pk = timeseries.peak(dense)
    trend = {
        "banner": {
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(pk["y"]) if pk else "—", "label": f"Peak index · {pk['x']}" if pk else "Peak index"},
                {"value": str(len(points)), "label": f"{cadence} points tracked"},
                {"value": str(len(spikes)), "label": "Spikes detected"},
            ],
        },
        "title": f"{cadence} brand loyalty index",
        "note": "",
        "unit": "Loyalty index",
        "points": points,
        "spikes": spikes,
        "footnote": (
            f"Index per {grain} over {brand_basis}; {grain}s with fewer than {MIN_COHORT} posts are not scored. "
            f"Spike = index above {timeseries.SPIKE_RATIO}× the trailing {timeseries.SPIKE_TRAILING}-{grain} mean."
        ),
    }
    band = band_for(idx_brand)
    monthly = {
        "score": idx_brand if idx_brand is not None else INDEX_MIN,
        "min": INDEX_MIN,
        "max": INDEX_MAX,
        "bands": [dict(b) for b in BANDS],
        "band": BAND_LABEL[band["tone"]] if band else None,
        "text": "",
    }
    rows = [{"name": "Industry score", "value": idx_industry if idx_industry is not None else 0}]
    if idx_comp is not None and comp_name:
        rows.append({"name": f"{comp_name} score", "value": idx_comp})
    rows.append({"name": f"{brand or 'Brand'} score", "value": idx_brand if idx_brand is not None else 0, "is_brand": True})
    over = idx_brand is not None and idx_industry is not None and idx_brand > idx_industry
    benchmark = {"rows": rows, "verdict": {"label": "Over indexed" if over else "Under indexed", "tone": "pos" if over else "neg"}}

    shares_brand = _bucket_shares(brand_rows, bucket_of)
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "top_competitor": comp_name,
            "window": window,
            "grain": grain,
            "total_mentions": total,
            "logos": brand_media.brand_logos([brand, *known], articles),
            "method": {
                "index": "10 + 90 * (W_adv*X_adv + W_sent*(net+100)/200 + W_use*X_use + W_sw*(1-X_sw))",
                "weights": dict(WEIGHTS),
                "weights_source": WEIGHTS_SOURCE,
                "brand_basis": brand_basis,
                "period_label": period_label,
                "nps_proxy": "%positive - %negative among brand posts on consumer-voice platforms (forums, reviews, X); all brand posts when too few",
                "net_sentiment": "%positive - %negative across every post in the window (category climate)",
                "switching": "share of brand posts naming two or more known brands",
                "usage": f"mean brand posts per {grain}",
            },
            "bucket_shares": {b: round(v * 100) for b, v in shares_brand.items()},
        },
        "footer": ["Shifting Audience Priorities · Advanced Metrics", f"Computed from {total:,} tagged posts"],
        "loyalty": loyalty,
        "trend": trend,
        "monthly": monthly,
        "benchmark": benchmark,
        "evidence": {
            # Real excerpts for the narrative to ground its prose in; never shown raw.
            "brand_quotes": quotes.pick(brand_rows, needles=[brand] if brand else None, limit=3),
            "competitor_quotes": quotes.pick(comp_rows, needles=[comp_name] if comp_name else None, limit=2),
            "top_groups": [{"name": n, "count": c, "bucket": bucket_of.get(n, "other")} for n, c in taxonomy.group_counts(brand_rows).most_common(6)],
        },
    }
