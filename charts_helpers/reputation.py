import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from db_helpers.schema import ChartResult

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PILLAR_WEIGHTS = {
    "Trust": 0.25,
    "Value": 0.15,
    "Advocacy": 0.20,
    "Social": 0.15,
    "Brand": 0.15,
    "Risk": 0.10,
}

# Internal weights within the Trust pillar (from Slide 5)
TRUST_INTERNAL = {"sentiment": 0.4, "prominence": 0.3, "tier1": 0.3}

# Domains we treat as Tier-1 outlets. Extend as needed.
TIER1_DOMAINS = {
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "nytimes.com",
    "washingtonpost.com", "cnbc.com", "forbes.com", "bbc.com", "cnn.com",
    "apnews.com", "theguardian.com", "economist.com", "axios.com",
    "globenewswire.com", "businesswire.com", "yahoo.com",
}

# Keyword buckets used only to map an article's free-form `theme` onto the
# pillars it most affects (for the theme × pillar heatmap and the Value /
# Advocacy / Social proxies). The article's own `theme` is the source of truth
# for grouping — these are just a lightweight classifier on top of it.
VALUE_KEYWORDS = ("financial", "earnings", "revenue", "sales", "stock",
                  "valuation", "ai", "robot", "autonomous", "innovation",
                  "product", "technology")
ADVOCACY_KEYWORDS = ("policy", "regulation", "mandate", "rebate", "government",
                     "advocacy", "lobby")
SOCIAL_KEYWORDS = ("policy", "regulation", "mandate", "community", "sustainab",
                   "social", "environment", "ai", "robot")


# ---------------------------------------------------------------------------
# Field accessors & small utilities (operate on a single article dict)
# ---------------------------------------------------------------------------

def _get_reach(item: dict) -> int:
    try:
        return int(item.get("reach") or 0)
    except (TypeError, ValueError):
        return 0


def _get_sentiment(item: dict) -> str:
    s = item.get("sentiment")
    return s if s in ("POS", "NEG", "NEU") else "NEU"


def _get_theme(item: dict) -> str:
    return str(item.get("theme") or "").strip()


def _get_domain(item: dict) -> str:
    return str(item.get("domain_name") or item.get("source") or item.get("url") or "").strip()


def _parse_date(item: dict) -> datetime | None:
    raw = (
        item.get("date")
        or item.get("published_date")
        or item.get("timestamp")
        or item.get("pubDate")
    )
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _is_tier1_article(item: dict) -> bool:
    domain = _get_domain(item).lower()
    if domain and any(d in domain for d in TIER1_DOMAINS):
        return True
    return _get_reach(item) > 1_000_000


def _theme_matches(item: dict, keywords) -> bool:
    text = f"{_get_theme(item)} {item.get('title') or ''}".lower()
    return any(k in text for k in keywords)


def _theme_pillars(theme: str) -> list:
    """Map a free-form theme onto the pillars it most affects (keyword heuristic)."""
    t = theme.lower()
    table = [
        (("lawsuit", "litigation", "recall", "crisis", "fraud", "investigation",
          "safety", "crash"), ["Risk", "Trust"]),
        (("policy", "regulation", "mandate", "rebate", "government"),
         ["Advocacy", "Social"]),
        (("leadership", "ceo", "executive", "management"), ["Brand", "Trust"]),
        (("ai", "robot", "autonomous", "innovation", "technology", "product"),
         ["Brand", "Value"]),
        (("earnings", "revenue", "sales", "financial", "stock", "valuation"),
         ["Value", "Trust"]),
        (("competitor", "market share", "rival"), ["Brand", "Value"]),
    ]
    for kws, pillars in table:
        if any(k in t for k in kws):
            return pillars
    return ["Brand"]


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _period_start(dt: datetime, freq: str) -> str:
    """Bucket a date into the start of its period as an ISO date string."""
    if freq == "W":
        start = dt - timedelta(days=dt.weekday())  # Monday
    elif freq == "M":
        start = dt.replace(day=1)
    else:  # "D" — daily
        start = dt
    return start.date().isoformat()


def _group_by_period(data: list, freq: str = "W") -> list:
    """Return [(period_iso, [articles])] sorted chronologically. Articles with
    an unparseable date are skipped."""
    buckets: dict[str, list] = defaultdict(list)
    for item in data:
        dt = _parse_date(item)
        if dt is None:
            continue
        buckets[_period_start(dt, freq)].append(item)
    return sorted(buckets.items())


# ---------------------------------------------------------------------------
# Core KPI calculations
# ---------------------------------------------------------------------------

def _normalize_sentiment(data: list) -> float:
    """Net Sentiment Score = ((Pos - Neg) / Total) * 100, scaled to 0-100."""
    if not data:
        return 50.0
    counts = {"POS": 0, "NEG": 0, "NEU": 0}
    for item in data:
        counts[_get_sentiment(item)] += 1
    raw = ((counts["POS"] - counts["NEG"]) / len(data)) * 100
    return float((raw + 100) / 2)


def _normalize_prominence(data: list) -> float:
    """Prominence Index = % of articles where the brand of interest is present."""
    if not data:
        return 0.0
    prominent = sum(1 for item in data if item.get("brand_of_interest"))
    return float((prominent / len(data)) * 100)


def _normalize_tier1(data: list) -> float:
    """Tier-1 Share = % of articles from Tier-1 sources (by domain or high reach)."""
    if not data:
        return 0.0
    tier1 = sum(1 for item in data if _is_tier1_article(item))
    return float((tier1 / len(data)) * 100)


def _negative_coverage_rate(data: list) -> float:
    """Negative Coverage Rate = % of articles that are negative in sentiment."""
    if not data:
        return 0.0
    negatives = sum(1 for item in data if _get_sentiment(item) == "NEG")
    return float((negatives / len(data)) * 100)


def compute_pillar_scores(data: list) -> dict:
    """Return all six pillar scores (0-100) for the given slice of articles."""
    sent = _normalize_sentiment(data)
    prom = _normalize_prominence(data)
    tier1 = _normalize_tier1(data)
    ncr = _negative_coverage_rate(data)
    n = max(len(data), 1)

    # Trust = 0.4*Sentiment + 0.3*Prominence + 0.3*Tier1
    trust = (TRUST_INTERNAL["sentiment"] * sent
             + TRUST_INTERNAL["prominence"] * prom
             + TRUST_INTERNAL["tier1"] * tier1)

    # Value: VNS + BPR + MPR averaged. VNS from value-themed coverage share.
    vns = sum(1 for i in data if _theme_matches(i, VALUE_KEYWORDS)) / n * 100
    value = (vns + sent + prom) / 3

    # Advocacy: SOV + PIM averaged. Proxy: policy-themed coverage share (scaled up).
    sov = min(sum(1 for i in data if _theme_matches(i, ADVOCACY_KEYWORDS)) / n * 100 * 4, 100)
    advocacy = (sov + sent) / 2

    # Social Impact: CIMV + ISR averaged.
    cimv = sum(1 for i in data if _theme_matches(i, SOCIAL_KEYWORDS)) / n * 100
    social = (cimv + sent) / 2

    # Brand: SER + RAI averaged. Reach-driven.
    reaches = [_get_reach(i) for i in data]
    median_reach = statistics.median(reaches) if reaches else 0
    reach_norm = _clip(math.log1p(median_reach) / math.log1p(1e6) * 100, 0, 100)
    brand = (sent + reach_norm) / 2

    # Risk: 100 - NCR
    risk = 100 - ncr

    return {
        "Trust": round(trust, 2),
        "Value": round(value, 2),
        "Advocacy": round(advocacy, 2),
        "Social": round(social, 2),
        "Brand": round(brand, 2),
        "Risk": round(risk, 2),
    }


def compute_reputation_index(pillars: dict) -> float:
    """RI = weighted sum of 6 pillar scores (Slide 5 formula)."""
    return round(sum(pillars[p] * PILLAR_WEIGHTS[p] for p in PILLAR_WEIGHTS), 2)


# ---------------------------------------------------------------------------
# Chart functions — one per chart, each returns JSON-serializable dict
# ---------------------------------------------------------------------------

class ReputationCharts:

    def ri_gauge(self, data: list, prev_data: list | None = None) -> ChartResult:
        """Headline gauge: current RI score with delta vs prior period."""
        pillars = compute_pillar_scores(data)
        ri = compute_reputation_index(pillars)
        delta = None
        if prev_data:
            prev_ri = compute_reputation_index(compute_pillar_scores(prev_data))
            delta = round(ri - prev_ri, 2)
        band = "green" if ri >= 70 else ("amber" if ri >= 50 else "red")

        chart_data = {
            "value": ri,
            "min": 0,
            "max": 100,
            "band": band,
            "delta": delta,
        }

        response = ChartResult(
            chart_id="ri_gauge",
            title="Reputation Index",
            description="Headline reputation index (0–100) for the current period with the delta versus the prior period.",
            chart_type="gauge",
            data=chart_data,
            series=[],
            x_label="",
            y_label="Reputation Index",
        )
        return response


    def ri_timeseries(self, data: list, freq: str = "W") -> ChartResult:
        """RI over time (weekly by default) with a moving average overlay."""
        points = []
        for period, items in _group_by_period(data, freq):
            ri = compute_reputation_index(compute_pillar_scores(items))
            points.append({"date": period, "ri": ri, "volume": len(items)})
        # 3-period moving average
        vals = [p["ri"] for p in points]
        for i, p in enumerate(points):
            window = vals[max(0, i - 2):i + 1]
            p["ri_ma"] = round(sum(window) / len(window), 2) if window else None

        chart_data = [
            { "name": "RI", "data": { p["date"]: p["ri"] for p in points }},
            { "name": "3-period MA", "data": { p["date"]: p["ri_ma"] for p in points }, "dashed": True },
        ]

        response = ChartResult(
            chart_id="ri_timeseries",
            title="Reputation Index Over Time",
            description="Weekly reputation index trend with a 3-period moving-average overlay.",
            chart_type="line",
            data=chart_data,
            series=["RI", "3-period MA"],
            x_label="Period",
            y_label="Reputation Index",
        )
        return response


    def ri_decomposition_coverage(self, data: list, freq: str = "W") -> ChartResult:
        """Stacked area: each band is a pillar's weighted contribution to RI.

        Returns one entry per pillar with a date→contribution map:
            [{"name": "Trust", "data": {"2026-01-05": 17.5, ...}}, ...]
        """
        contribs: dict[str, dict[str, float]] = {p: {} for p in PILLAR_WEIGHTS}
        for period, items in _group_by_period(data, freq):
            pillars = compute_pillar_scores(items)
            for p, w in PILLAR_WEIGHTS.items():
                contribs[p][period] = round(pillars[p] * w, 2)

        chart_data = [{"name": p, "data": contribs[p]} for p in PILLAR_WEIGHTS]

        return ChartResult(
            chart_id="ri_decomposition_coverage",
            title="RI Decomposition by Pillar",
            description="Stacked area of each pillar's weighted contribution to the Reputation Index over time.",
            chart_type="stacked_area",
            data=chart_data,
            series=list(PILLAR_WEIGHTS.keys()),
            x_label="Period",
            y_label="Weighted Contribution",
        )


    def pillar_radar(self, data: list, compare_data: list | None = None) -> ChartResult:
        """Radar / spider chart of the six pillars for the current period.

        Returns one entry per series with a pillar→score map:
            [{"name": "Current", "values": {"Trust": 65.2, ...}}, {"name": "Previous", ...}]
        """
        pillars = compute_pillar_scores(data)
        chart_data = [{"name": "Current", "values": {p: pillars[p] for p in PILLAR_WEIGHTS}}]
        if compare_data:
            prev_pillars = compute_pillar_scores(compare_data)
            chart_data.append({"name": "Previous", "values": {p: prev_pillars[p] for p in PILLAR_WEIGHTS}})

        return ChartResult(
            chart_id="pillar_radar",
            title="Pillar Scores Radar",
            description="Radar comparison of the six pillar scores for the current versus previous period.",
            chart_type="radar",
            data=chart_data,
            series=[s["name"] for s in chart_data],
            x_label="Pillar",
            y_label="Score",
        )


    def pillar_bar(self, data: list) -> ChartResult:
        """Horizontal bar of current pillar scores, ranked."""
        pillars = compute_pillar_scores(data)
        ranked = sorted(pillars.items(), key=lambda kv: kv[1], reverse=True)
        chart_data = [{"pillar": p, "score": s} for p, s in ranked]

        return ChartResult(
            chart_id="pillar_bar",
            title="Pillar Scores (Ranked)",
            description="Current pillar scores ranked from highest to lowest.",
            chart_type="bar",
            data=chart_data,
            series=[],
            x_label="Score",
            y_label="Pillar",
        )


    def trust_kpi_breakdown(self, data: list) -> ChartResult:
        """The three normalized KPIs that build the Trust pillar."""
        chart_data = [
            {"kpi": "Total Count", "score": len(data), "weight": None},
            {"kpi": "Net Sentiment", "score": round(_normalize_sentiment(data), 2), "weight": 0.4},
            {"kpi": "Prominence Index", "score": round(_normalize_prominence(data), 2), "weight": 0.3},
            {"kpi": "Tier-1 Coverage", "score": round(_normalize_tier1(data), 2), "weight": 0.3},
        ]

        return ChartResult(
            chart_id="trust_kpi_breakdown",
            title="Trust KPI Breakdown",
            description="The normalized KPIs that build the Trust pillar, with their internal weights.",
            chart_type="bar",
            data=chart_data,
            series=[],
            x_label="KPI",
            y_label="Score",
        )


    def trust_waterfall(self, data: list) -> ChartResult:
        """Waterfall: how the three weighted KPIs build up to the Trust Score."""
        sent = _normalize_sentiment(data) * TRUST_INTERNAL["sentiment"]
        prom = _normalize_prominence(data) * TRUST_INTERNAL["prominence"]
        tier1 = _normalize_tier1(data) * TRUST_INTERNAL["tier1"]
        total = sent + prom + tier1
        chart_data = [
            {"label": "Start", "value": 0, "kind": "start"},
            {"label": "0.4 × Sentiment", "value": round(sent, 2), "kind": "add"},
            {"label": "0.3 × Prominence", "value": round(prom, 2), "kind": "add"},
            {"label": "0.3 × Tier-1", "value": round(tier1, 2), "kind": "add"},
            {"label": "Trust Score", "value": round(total, 2), "kind": "total"},
        ]

        return ChartResult(
            chart_id="trust_waterfall",
            title="Trust Score Waterfall",
            description="How the three weighted KPIs build up to the Trust pillar score.",
            chart_type="waterfall",
            data=chart_data,
            series=[],
            x_label="Component",
            y_label="Score",
        )


    def sentiment_coverage(self, data: list, freq: str = "W") -> ChartResult:
        """Stacked bars of POS/NEU/NEG volume per period.

        Returns one entry per period:
            [{"date": "2026-02-02", "POS": 2, "NEU": 0, "NEG": 1}, ...]
        """
        chart_data = []
        for period, items in _group_by_period(data, freq):
            counts = {"POS": 0, "NEU": 0, "NEG": 0}
            for item in items:
                counts[_get_sentiment(item)] += 1
            chart_data.append({"date": period, **counts})

        return ChartResult(
            chart_id="sentiment_coverage",
            title="Sentiment Coverage Over Time",
            description="Stacked volume of positive, neutral, and negative articles per period.",
            chart_type="stacked_bar",
            data=chart_data,
            series=["POS", "NEU", "NEG"],
            x_label="Period",
            y_label="Article Volume",
        )


    def net_sentiment_coverage(self, data: list, freq: str = "W") -> ChartResult:
        """Net Sentiment Score over time (the exact KPI from Slide 5).

        Returns a date→NSS map: {"2026-02-02": 33.33, ...}
        """
        chart_data = {}
        for period, items in _group_by_period(data, freq):
            pos = sum(1 for i in items if _get_sentiment(i) == "POS")
            neg = sum(1 for i in items if _get_sentiment(i) == "NEG")
            chart_data[period] = round(((pos - neg) / len(items)) * 100, 2) if items else 0

        return ChartResult(
            chart_id="net_sentiment_coverage",
            title="Net Sentiment Over Time",
            description="Net Sentiment Score ((Positive − Negative) / Total × 100) per period.",
            chart_type="line",
            data=chart_data,
            series=[],
            x_label="Period",
            y_label="Net Sentiment Score",
        )


    def coverage_volume(self, data: list, freq: str = "W") -> ChartResult:
        """Volume of articles + total reach per period.

        Returns one entry per period:
            [{"date": "2026-02-02", "total_count": 100, "total_reach": 500000}, ...]
        """
        chart_data = [
            {
                "date": period,
                "total_count": len(items),
                "total_reach": sum(_get_reach(i) for i in items),
            }
            for period, items in _group_by_period(data, freq)
        ]

        return ChartResult(
            chart_id="coverage_volume",
            title="Coverage Volume & Reach",
            description="Article count and total reach per period.",
            chart_type="bar",
            data=chart_data,
            series=["total_count", "total_reach"],
            x_label="Period",
            y_label="Volume / Reach",
        )


    def tier1_share(self, data: list, freq: str = "W") -> ChartResult:
        """Stacked bar: Tier-1 vs non-Tier-1 share of coverage over time.

        Returns one entry per period:
            [{"date": "2026-02-02", "Tier 1": 1, "Other": 2}, ...]
        """
        chart_data = []
        for period, items in _group_by_period(data, freq):
            t1 = sum(1 for i in items if _is_tier1_article(i))
            chart_data.append({"date": period, "Tier 1": t1, "Other": len(items) - t1})

        return ChartResult(
            chart_id="tier1_share",
            title="Tier-1 Coverage Share",
            description="Tier-1 versus non-Tier-1 share of coverage over time.",
            chart_type="stacked_bar",
            data=chart_data,
            series=["Tier 1", "Other"],
            x_label="Period",
            y_label="Article Volume",
        )


    def source_treemap(self, data: list, top_n: int = 15) -> ChartResult:
        """Treemap: rectangle size = reach, color = net sentiment per source."""
        stats: dict[str, dict] = defaultdict(lambda: {"articles": 0, "reach": 0, "pos": 0, "neg": 0})
        for item in data:
            source = _get_domain(item) or "Unknown"
            s = stats[source]
            s["articles"] += 1
            s["reach"] += _get_reach(item)
            sentiment = _get_sentiment(item)
            if sentiment == "POS":
                s["pos"] += 1
            elif sentiment == "NEG":
                s["neg"] += 1

        rows = []
        for source, s in stats.items():
            net = round((s["pos"] - s["neg"]) / s["articles"] * 100, 2) if s["articles"] else 0
            rows.append({
                "source": source,
                "reach": int(s["reach"]),
                "articles": int(s["articles"]),
                "net_sentiment": net,
            })
        rows.sort(key=lambda r: r["reach"], reverse=True)

        return ChartResult(
            chart_id="source_treemap",
            title="Source Treemap",
            description="Top sources sized by reach and colored by net sentiment.",
            chart_type="treemap",
            data=rows[:top_n],
            series=[],
            x_label="",
            y_label="",
        )


    def theme_volume(self, data: list, top_n: int = 10) -> ChartResult:
        """Narrative themes — volume + net sentiment per theme (from the `theme` field)."""
        stats: dict[str, dict] = defaultdict(lambda: {"volume": 0, "pos": 0, "neg": 0})
        for item in data:
            theme = _get_theme(item)
            if not theme:
                continue
            s = stats[theme]
            s["volume"] += 1
            sentiment = _get_sentiment(item)
            if sentiment == "POS":
                s["pos"] += 1
            elif sentiment == "NEG":
                s["neg"] += 1

        rows = []
        for theme, s in stats.items():
            net = round((s["pos"] - s["neg"]) / s["volume"] * 100, 2) if s["volume"] else 0
            rows.append({"theme": theme, "volume": s["volume"], "net_sentiment": net})
        rows.sort(key=lambda r: r["volume"], reverse=True)

        return ChartResult(
            chart_id="theme_volume",
            title="Narrative Themes",
            description="Volume and net sentiment per narrative theme.",
            chart_type="bar",
            data=rows[:top_n],
            series=[],
            x_label="Theme",
            y_label="Volume",
        )


    def theme_pillar_heatmap(self, data: list, top_n: int = 10) -> ChartResult:
        """Heatmap: themes × pillars, cell value = article volume.

        Returns one entry per theme with a pillar→count map:
            [{"theme": "Securities Litigation Outcome", "values": {"Trust": 0, "Risk": 4, ...}}, ...]
        """
        pillars = list(PILLAR_WEIGHTS.keys())
        theme_counts: dict[str, int] = defaultdict(int)
        for item in data:
            theme = _get_theme(item)
            if theme:
                theme_counts[theme] += 1

        top_themes = sorted(theme_counts, key=theme_counts.get, reverse=True)[:top_n]
        chart_data = []
        for theme in top_themes:
            n = theme_counts[theme]
            affected = _theme_pillars(theme)
            values = {p: (n if p in affected else 0) for p in pillars}
            chart_data.append({"theme": theme, "values": values})

        return ChartResult(
            chart_id="theme_pillar_heatmap",
            title="Theme × Pillar Heatmap",
            description="Article volume by theme across the pillars each theme most affects.",
            chart_type="heatmap",
            data=chart_data,
            series=pillars,
            x_label="Pillar",
            y_label="Theme",
        )


    def risk_negative_coverage(self, data: list, freq: str = "W", threshold: float = 25.0) -> ChartResult:
        """Risk view: negative coverage rate over time.

        Returns a date→NCR map: {"2026-02-02": 33.33, ...}
        """
        chart_data = {}
        for period, items in _group_by_period(data, freq):
            chart_data[period] = round(sum(1 for i in items if _get_sentiment(i) == "NEG") / len(items) * 100, 2) if items else 0

        return ChartResult(
            chart_id="risk_negative_coverage",
            title="Negative Coverage Rate",
            description="Share of negative articles over time — a leading risk indicator.",
            chart_type="line",
            data=chart_data,
            series=[],
            x_label="Period",
            y_label="Negative Coverage Rate (%)",
        )


    def pillar_small_multiples(self, data: list, freq: str = "W") -> ChartResult:
        """Six mini line charts, one per pillar, sharing the same time axis.

        Returns one entry per pillar with a date→score map:
            [{"name": "Trust", "data": {"2026-02-02": 65.2, ...}}, ...]
        """
        series_data: dict[str, dict[str, float]] = {p: {} for p in PILLAR_WEIGHTS}
        for period, items in _group_by_period(data, freq):
            pillars = compute_pillar_scores(items)
            for p in PILLAR_WEIGHTS:
                series_data[p][period] = pillars[p]

        chart_data = [{"name": p, "data": series_data[p]} for p in PILLAR_WEIGHTS]

        return ChartResult(
            chart_id="pillar_small_multiples",
            title="Pillar Trends (Small Multiples)",
            description="Per-pillar score trends sharing a common time axis.",
            chart_type="line",
            data=chart_data,
            series=list(PILLAR_WEIGHTS.keys()),
            x_label="Period",
            y_label="Score",
        )


    def weight_sensitivity(self, data: list, delta: float = 0.10) -> ChartResult:
        """Tornado: how much RI moves when each pillar weight shifts ±10%."""
        pillars = compute_pillar_scores(data)
        base_ri = compute_reputation_index(pillars)
        rows = []
        for pillar, base_w in PILLAR_WEIGHTS.items():
            # Increase the pillar's weight by `delta`, reduce others proportionally
            for direction, factor in [("up", 1 + delta), ("down", 1 - delta)]:
                new_w = base_w * factor
                others_total = 1 - new_w
                scale = others_total / (1 - base_w) if (1 - base_w) > 0 else 0
                new_weights = {p: (new_w if p == pillar else PILLAR_WEIGHTS[p] * scale)
                            for p in PILLAR_WEIGHTS}
                new_ri = sum(pillars[p] * new_weights[p] for p in PILLAR_WEIGHTS)
                rows.append({"pillar": pillar, "direction": direction, "ri": round(new_ri, 2),
                            "delta": round(new_ri - base_ri, 2)})

        return ChartResult(
            chart_id="weight_sensitivity",
            title="Weight Sensitivity (Tornado)",
            description="How the Reputation Index moves when each pillar weight shifts ±10%.",
            chart_type="tornado",
            data=rows,
            series=[],
            x_label="RI Delta",
            y_label="Pillar",
        )


    # ---------------------------------------------------------------------------
    # Aggregate: build all charts at once for the dashboard
    # ---------------------------------------------------------------------------
    def current_previous_dates(self, data: list):
        dates = [d for d in (_parse_date(i) for i in data) if d is not None]
        max_date = max(dates) if dates else None
        min_date = min(dates) if dates else None

        # Split into "current" (last 4 weeks) and "previous" (before that) for deltas.
        if max_date is not None:
            cutoff = max_date - timedelta(weeks=4)
            current = [i for i in data if (_parse_date(i) or datetime.min) > cutoff]
            previous = [i for i in data if (_parse_date(i) or datetime.min) <= cutoff]
            if len(current) < 3:  # too few — just use all
                current, previous = data, None
        else:
            current, previous = data, None
        
        return current, previous

reputation_charts = ReputationCharts()