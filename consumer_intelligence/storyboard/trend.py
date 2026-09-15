"""Trend Intelligence storyboard — news-article adaptation.

Faithful port of ConsumerIntelligence_PR/backend/app/charts/storyboard/trend.py:
a three-tab narrative rather than a grid of charts. Everything numeric is
computed here; every prose field is left empty for `narrative.py` to fill, so a
failed or malformed LLM response degrades to a storyboard with real numbers and
no commentary — never a broken screen.

Adaptations (field access only):
- signal    → aggregate.signal_label()   (subtheme → theme)
- platform  → aggregate.source_platform() (source_type → section → domain)
- engagement→ aggregate.engagement()      (falls back to reach)
"""

from datetime import datetime

from .. import aggregate, brand_media
from . import brands as brand_metrics
from . import signals as signal_metrics

LENS_KEY = "trend_intelligence"

MAX_PLATFORMS = 5  # the heatmap is laid out for five columns
MAX_QUOTES = 3
MAX_PRIORITIES = 3

# Modal keys the screen can open that are not a signal deep-dive.
CHART_MODALS = ("sov", "netsent", "signal_all", "phase", "momentum", "leaders", "heatmap")


def _fmt(value: int | float) -> str:
    return f"{value:,}"


def _day_label(day: str) -> str:
    """2026-06-25 -> Jun 25, matching the storyboard's axis labels."""
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    return f"{parsed.strftime('%b')} {parsed.day}"


def _window(articles: list[dict]) -> tuple[list[str], list[str]]:
    """Ordered calendar days present in the data, with their display labels."""
    days = sorted(d for d in aggregate.bucket_by_day(articles) if d != aggregate.UNKNOWN_DAY)
    return days, [_day_label(d) for d in days]


def _platforms(articles: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    for article in articles:
        name = aggregate.source_platform(article)
        if name and not aggregate.is_junk(name):
            counts[name] = counts.get(name, 0) + 1
    return [name for name, _ in aggregate.top_n(counts, MAX_PLATFORMS)]


def _quotes(articles: list[dict], signals: list[dict]) -> list[dict]:
    """Real excerpts evidencing the accelerating signals.

    Picked by engagement so the quote shown is one that actually travelled, and
    capped at one per signal so three cards do not all quote the same trend.
    """
    picked: list[dict] = []
    for signal in signals[:MAX_QUOTES]:
        candidates = [
            a
            for a in articles
            if aggregate.signal_label(a) == signal["name"] and (a.get("content") or a.get("title"))
        ]
        if not candidates:
            continue
        best = max(candidates, key=aggregate.engagement)
        text = str(best.get("content") or best.get("title") or "").strip()
        if len(text) > 220:
            text = text[:217].rstrip() + "…"
        source = " · ".join(
            part
            for part in (
                best.get("source_name") or aggregate.source_platform(best),
                signal["name"],
                best.get("sentiment"),
            )
            if part
        )
        picked.append({"text": text, "source": source})
    return picked


def _priorities(signals: list[dict]) -> list[dict]:
    """The signals worth acting on, ranked by strategic weight rather than volume.

    Three equally-weighted components, each capped at 10 for a score out of 30:
    momentum (is it growing), affection (is the conversation warm), and
    ownability (does the brand already have a foothold to build on). Volume is
    deliberately excluded — it is what the storyboard argues against ranking by.
    """
    def score(signal: dict) -> int:
        momentum = max(0.0, min(10.0, signal["growth"] / 4))
        affection = max(0.0, min(10.0, (signal["net_sentiment"] + 100) / 20))
        ownability = max(0.0, min(10.0, signal["brand_capture"] / 3))
        return round(momentum + affection + ownability)

    ranked = sorted(signals, key=lambda s: -score(s))[:MAX_PRIORITIES]
    return [
        {
            "num": f"{index + 1:02d}",
            "name": signal["name"],
            "score": f"Score: {score(signal)} / 30",
            "stage": signal["stage"],
            "stage_label": signal["stage_label"],
            "why": "",
            "signal_id": signal["id"],
        }
        for index, signal in enumerate(ranked)
    ]


# Ported from the storyboard's four k-accent flip cards.
KPI_ACCENTS = [
    ["#7c3aed", "#a855f7"],
    ["#3b82f6", "#60a5fa"],
    ["#ec4899", "#f472b6"],
    ["#702082", "#6c2bd9"],
]


def _kpis(signals: list[dict]) -> list[dict]:
    """The four headline cards. Values are computed; only `back` is LLM-written.

    Keeping the figures out of the model's hands is what stops a flip card from
    disagreeing with the chart directly beneath it.
    """
    if not signals:
        return []

    fastest = max(signals, key=lambda s: s["growth"])
    warmest = max(signals, key=lambda s: s["net_sentiment"])
    biggest = max(signals, key=lambda s: s["volume"])
    weakest = min(signals, key=lambda s: s["growth"])

    picks = [
        ("Fastest-growing signal", fastest, f"{fastest['growth']:+d}% share growth"),
        ("Warmest signal", warmest, f"{warmest['net_sentiment']:+.1f} net sentiment"),
        ("Largest conversation", biggest, f"{_fmt(biggest['volume'])} conversations"),
        ("Losing the most ground", weakest, f"{weakest['growth']:+d}% share growth"),
    ]
    return [
        {
            "label": label,
            "value": signal["name"],
            "delta": delta,
            "direction": "up" if not delta.startswith("-") else "down",
            "accent": KPI_ACCENTS[index % len(KPI_ACCENTS)],
            "back": "",
        }
        for index, (label, signal, delta) in enumerate(picks)
    ]


def _verdict_columns(signals: list[dict], *, mode: str) -> list[dict]:
    """Split the signals into ground to hold and ground to give up.

    In a category export the cut is the brand's mean capture, so the split reflects
    its real competitive position. In a brand export capture is uniform and would
    put every signal in one column, so momentum makes the honest cut instead.
    """
    if not signals:
        return []

    if mode == "category":
        mean = sum(s["brand_capture"] for s in signals) / len(signals)
        hold = [s for s in signals if s["brand_capture"] >= mean]
        cede = [s for s in signals if s["brand_capture"] < mean]
        titles = ("Defend & Contest", "Concede or Flank")
    else:
        hold = [s for s in signals if s["growth"] >= 0]
        cede = [s for s in signals if s["growth"] < 0]
        titles = ("Building Momentum", "Losing Ground")

    return [
        {
            "tone": "positive",
            "title": titles[0],
            "items": [{"label": s["name"], "text": ""} for s in hold],
        },
        {
            "tone": "negative",
            "title": titles[1],
            "items": [{"label": s["name"], "text": ""} for s in cede],
        },
    ]


def _modals(signals: list[dict], sov: list[dict], league: list[dict]) -> dict:
    """Deep-dive skeletons. Stats are computed; paragraphs are LLM-written."""
    modals: dict[str, dict] = {}

    for signal in signals:
        leader = signal["leaders"][0] if signal["leaders"] else None
        modals[signal["id"]] = {
            "tag": f"Trend · {signal['name']}",
            "title": f"{signal['name']} — {signal['growth']:+d}% Share Growth",
            "stats": [
                {"value": f"{signal['growth']:+d}%", "label": "Share growth"},
                {"value": _fmt(signal["volume"]), "label": "Conversations"},
                {"value": f"{signal['net_sentiment']:+.1f}", "label": "Net sentiment"},
                {"value": f"{signal['brand_capture']}%", "label": "Brand capture"},
            ],
            "paragraphs": [],
            "leader": f"{leader['brand']} ({leader['count']})" if leader else "",
        }

    def top_stats(rows: list[dict], suffix: str) -> list[dict]:
        return [
            {"value": f"{row['value']}{suffix}", "label": row["brand"]} for row in rows[:3]
        ]

    fastest = max(signals, key=lambda s: s["growth"], default=None)
    modals["sov"] = {
        "tag": "Snapshot · Share of Voice",
        "title": "Category Share of Voice",
        "stats": top_stats(sov, "%"),
        "paragraphs": [],
    }
    modals["netsent"] = {
        "tag": "Snapshot · Sentiment",
        "title": "Net Sentiment League Table",
        "stats": top_stats(league, ""),
        "paragraphs": [],
    }
    modals["signal_all"] = {
        "tag": "The Trends · Overview",
        "title": "Signal Trajectory",
        "stats": [
            {"value": str(len(signals)), "label": "Signals"},
            {
                "value": f"{fastest['growth']:+d}%" if fastest else "—",
                "label": f"Fastest ({fastest['name']})" if fastest else "Fastest",
            },
        ],
        "paragraphs": [],
    }
    for key, tag, title in (
        ("phase", "The Trends · Phase", "Daily Conversation Phase"),
        ("momentum", "The Trends · Momentum", "Cumulative Share Momentum"),
        ("leaders", "Competitive Position", "Who Leads Each Trend"),
        ("heatmap", "Competitive Position · Channel", "Signal × Platform Heatmap"),
    ):
        modals[key] = {"tag": tag, "title": title, "stats": [], "paragraphs": []}
    return modals


def _tabs(brand: str, kpis: list[dict]) -> list[dict]:
    """The three-tab skeleton. Every prose field is filled by narrative.py."""
    def shell(tab_id, label, number, banner_class, tone, icon, sections):
        return {
            "id": tab_id,
            "label": label,
            "number": number,
            "banner_class": banner_class,
            "banner": {
                "eyebrow": f"{number} · {label}",
                "headline": "",
                "sub": "",
                "badges": [],
                "image": None,
            },
            "context": {
                "tone": tone,
                "icon": icon,
                "heading": "Why This Intelligence Matters",
                "body": "",
            },
            "sections": sections,
            "kpis": kpis if tab_id == "tab1" else [],
            "whats_next": {"eyebrow": "", "title": "", "sub": "", "actions": [], "cta": None},
        }

    return [
        shell("tab1", "Snapshot", "01", "b-snapshot", "brand", "🎉", ["kpis", "sov_charts"]),
        shell("tab2", "The Trends", "02", "b-trends", "warning", "📈",
              ["signal_cards", "trajectory", "phase_momentum", "quotes"]),
        shell("tab3", f"{brand} in the Space" if brand else "Competitive Position", "03",
              "b-position", "risk", "🎯",
              ["capture", "callout", "leaders_heatmap", "priorities", "verdict"]),
    ]


def build_storyboard(
    articles: list[dict],
    *,
    brand: str,
    known_brands: list[str],
) -> dict:
    """Compute the full storyboard payload, prose fields left empty."""
    days, day_labels = _window(articles)
    platforms = _platforms(articles)

    signals = signal_metrics.build_signals(
        articles, days=days, platforms=platforms, brand=brand, known_brands=known_brands
    )
    sov = brand_metrics.share_of_voice(articles, known_brands)
    league = brand_metrics.net_sentiment_league(articles, known_brands)
    mode = brand_metrics.dataset_mode(sov)
    capture, capture_label = brand_metrics.capture_ranking(
        signals, mode=mode, total=len(articles)
    )

    competitors = [b for b in known_brands if b and b != brand]
    window = (
        f"{day_labels[0]} – {day_labels[-1]} {days[-1][:4]}" if day_labels else "No dated coverage"
    )

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": competitors,
            "total_conversations": len(articles),
            "window_label": window,
            "days": day_labels,
            "platforms": platforms,
            "dataset_mode": mode,
            "capture_label": capture_label,
            "logos": brand_media.brand_logos([brand, *known_brands], articles),
        },
        "hero": {
            "eyebrow": window,
            "chips": [],
            "title": "",
            "subtitle": "",
            "stats": [
                {"value": _fmt(len(articles)), "label": "Conversations", "animate": True},
                *(
                    [
                        {
                            "value": f"{signals[0]['growth']:+d}%",
                            "label": f"Fastest signal ({signals[0]['name']})",
                        }
                    ]
                    if signals
                    else []
                ),
                *(
                    [{"value": f"{sov[0]['value']}%", "label": f"{sov[0]['brand']} share of voice"}]
                    if sov
                    else []
                ),
                *(
                    [
                        {
                            "value": f"{next((r['value'] for r in league if r['brand'] == brand), 0):+.1f}",
                            "label": f"{brand} net sentiment",
                        }
                    ]
                    if brand and league
                    else []
                ),
            ],
            "media": None,
        },
        "signals": signals,
        "sov": sov,
        "net_sentiment": league,
        "leaders_matrix": brand_metrics.leaders_matrix(signals),
        "capture_ranking": capture,
        "tabs": _tabs(brand, _kpis(signals)),
        "quotes": _quotes(articles, signals),
        "callout": "",
        "priorities": _priorities(signals),
        "verdict_columns": _verdict_columns(signals, mode=mode),
        "modals": _modals(signals, sov, league),
        "footer": [],
    }
