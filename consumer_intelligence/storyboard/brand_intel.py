"""Brand Intelligence storyboard — news-article adaptation.

Port of ConsumerIntelligence_PR/backend/app/charts/storyboard/brand_intel.py.
Adaptation: brand_media module removed (media=None always), no imagery resolver.
Brandfetch logo URL added to leader cards via article domain extraction.
"""

import re

from .. import aggregate
from .. import brand_media

LENS_KEY = "brand_intelligence"

MAX_THEMES = 5
TOP_LEADERS = 3
MAX_VERBATIMS = 4
VERBATIM_CHARS = 260
MIN_THEME_VOLUME = 3
GROWTH_CAP = 300
GROWTH_MIN_VOLUME = 10


def _fmt(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(int(n))


def _theme_field(articles: list[dict]) -> str:
    for field in ("subtheme", "theme"):
        counts = aggregate.count_by(articles, field, skip_junk=True)
        if len(counts) >= 2:
            return field
    return "theme"


def _distinct_themes(counts: dict[str, int]) -> list[str]:
    stop = {"and", "the", "for", "of", "a", "an", "to", "in", "on", "with", "deals", "deal"}

    def keywords(name: str) -> frozenset[str]:
        return frozenset(
            w for w in name.lower().replace("&", " ").split() if w not in stop and len(w) > 2
        )

    kept: list[str] = []
    kept_keys: list[frozenset[str]] = []
    for name, n in aggregate.top_n(counts, MAX_THEMES * 3):
        if n < MIN_THEME_VOLUME:
            continue
        key = keywords(name)
        if key and any(key <= seen or seen <= key for seen in kept_keys):
            continue
        kept.append(name)
        kept_keys.append(key)
        if len(kept) >= MAX_THEMES:
            break
    return kept


def _window(articles: list[dict]) -> tuple[str | None, str | None]:
    days = sorted(d for d in (aggregate.parse_day(a.get("date")) for a in articles) if d)
    return (days[0], days[-1]) if days else (None, None)


def _growth(theme_articles: list[dict], all_days: list[str], start: str | None, end: str | None) -> dict:
    days = [aggregate.parse_day(a.get("date")) for a in theme_articles]
    days = [d for d in days if d]
    if all_days and days and start != end:
        pivot = all_days[len(all_days) // 2]
        early = sum(1 for d in days if d < pivot)
        late = len(days) - early
    else:
        early = late = 0

    if early and (early + late) >= GROWTH_MIN_VOLUME:
        delta = round((late - early) * 100 / early)
        delta = max(-GROWTH_CAP, min(GROWTH_CAP, delta))
        has_delta = True
    else:
        delta = 0
        has_delta = False

    return {
        "from_label": (start or "Start")[:10],
        "from_val": early,
        "to_label": (end or "Now")[:10],
        "to_val": late,
        "delta_pct": int(delta),
        "has_delta": has_delta,
    }


def _sentiment_split(articles: list[dict]) -> list[dict]:
    rated = [a for a in articles if a.get("sentiment")]
    total = len(rated) or 1
    rows = []
    for label, tone in (("Positive", "pos"), ("Neutral", "neu"), ("Negative", "neg")):
        n = sum(1 for a in rated if a["sentiment"] == label)
        rows.append({"label": label, "pct": round(n * 100 / total), "tone": tone, "count": n})
    return [r for r in rows if r["tone"] != "neu" or r["count"]]


def _extract_domain(brand_name: str, articles: list[dict]) -> str | None:
    """Try to extract a usable domain from article URLs mentioning the brand."""
    brand_lower = brand_name.lower()
    for article in articles:
        url = str(article.get("url") or "")
        if not url:
            continue
        m = re.search(r"https?://(?:www\.)?([^/]+)", url)
        if m:
            domain = m.group(1)
            if brand_lower.split()[0] in domain.lower():
                return domain
    return None


def _leaders(
    theme_articles: list[dict],
    brand: str,
    known: list[str],
    all_articles: list[dict],
) -> list[dict]:
    counts: dict[str, int] = {}
    for a in theme_articles:
        for name in aggregate.brands_in(a, known):
            counts[name] = counts.get(name, 0) + 1
    ranked = aggregate.top_n(counts, TOP_LEADERS + 1)
    out = []
    for name, n in ranked:
        if len(out) >= TOP_LEADERS:
            break
        is_subject = name.lower() == (brand or "").lower()
        # Try to get logo URL via Brandfetch CDN pattern
        domain = _extract_domain(name, all_articles)
        logo_url = brand_media.brandfetch_logo_url(domain) if domain else None
        out.append(
            {
                "name": name,
                "mentions": n,
                "is_subject": is_subject,
                "desc": "",
                "badge": "Most cited" if not out else "",
                "logo_url": logo_url,
                "media": None,
            }
        )
    return out


_BREAKS = re.compile(r">>+|[\r\n]+|•|\|")
_SENTENCES = re.compile(r"[^.!?]*[.!?]|[^.!?]+$")


def _brand_snippet(text: str, needle: str) -> str:
    if not needle:
        return ""
    sentences: list[str] = []
    for segment in _BREAKS.split(text):
        for match in _SENTENCES.findall(segment):
            s = match.strip()
            if s:
                sentences.append(s)
    hits = [s for s in sentences if needle in s[:140].lower()]
    if not hits:
        return ""
    snippet = " ".join(hits[:2]).strip()
    if len(snippet) > VERBATIM_CHARS:
        snippet = snippet[:VERBATIM_CHARS].rsplit(" ", 1)[0] + "…"
    return snippet


def _verbatims(theme_articles: list[dict], brand: str) -> list[dict]:
    needle = (brand or "").lower()
    if not needle:
        return []
    candidates: list[tuple[bool, int, str, str]] = []
    for a in theme_articles:
        raw = str(a.get("content") or a.get("summary") or a.get("title") or "")
        snippet = _brand_snippet(raw, needle)
        if len(snippet) < 40:
            continue
        authored = bool(str(a.get("author") or "").strip())
        author = str(a.get("author") or "").strip()
        channel = str(a.get("source_type") or a.get("section") or "News").strip()
        source = f"{author} · {channel}" if author else channel
        candidates.append((authored, -abs(len(snippet) - 170), snippet, source))

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    out, seen = [], set()
    for _authored, _score, snippet, source in candidates:
        key = snippet[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": snippet, "source": source})
        if len(out) >= MAX_VERBATIMS:
            break
    return out


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    known = known_brands or ([brand] if brand else [])
    start, end = _window(articles)
    window_label = f"{start} – {end}" if start and end else "Capture window"
    dataset_mode = "brand" if len({*known}) <= 1 else "category"

    all_days = sorted({d for a in articles if (d := aggregate.parse_day(a.get("date")))})
    field = _theme_field(articles)
    theme_counts = aggregate.count_by(articles, field, skip_junk=True)
    ranked_themes = _distinct_themes(theme_counts)

    total = len(articles)
    engaged = sum(aggregate.engagement(a) for a in articles)
    reach = sum(aggregate.reach(a) for a in articles)
    net = aggregate.net_sentiment(articles)

    # Brand logo for hero
    brand_domain = _extract_domain(brand, articles)
    brand_logo_url = brand_media.brandfetch_logo_url(brand_domain) if brand_domain else None

    hero = {
        "eyebrow": "Brand Intelligence Study",
        "title": f"What's Shaping the {brand or 'Category'} Conversation",
        "title_em": brand or "Category",
        "date_label": window_label,
        "subtitle": "",
        "logo_url": brand_logo_url,
        "kpis": [
            {"value": _fmt(total), "label": "Mentions analysed"},
            {"value": f"{len(ranked_themes)}", "label": "Trends identified"},
            {"value": f"{'+' if net >= 0 else ''}{net}", "label": "Net sentiment"},
            {"value": _fmt(engaged), "label": "Total engagement"},
            {"value": _fmt(reach), "label": "Total reach"},
        ],
    }

    tabs: list[dict] = []
    for i, theme in enumerate(ranked_themes, start=1):
        ta = [a for a in articles if str(a.get(field) or "").strip() == theme]
        te = sum(aggregate.engagement(a) for a in ta)
        growth = _growth(ta, all_days, start, end)
        tabs.append(
            {
                "id": f"t{i}",
                "number": f"{i:02d}",
                "label": theme,
                "banner": {
                    "tag": f"Trend {i:02d} · {theme}",
                    "title": theme,
                    "title_em": "",
                    "stat": (
                        f"{'+' if growth['delta_pct'] >= 0 else ''}{growth['delta_pct']}%"
                        if growth["has_delta"]
                        else f"{len(ta)} mentions"
                    ),
                    "desc": "",
                    "image": None,
                },
                "context": "",
                "growth": growth,
                "sentiment": _sentiment_split(ta),
                "leaders": {
                    "tag": "Brands Leading This Space",
                    "engagement": _fmt(te),
                    "reviews": len(ta),
                    "items": _leaders(ta, brand, known, articles),
                },
                "verbatims": _verbatims(ta, brand),
                "whats_next": {"label": "Next trend", "title": "", "sub": "", "cta": ""},
            }
        )

    cards = [
        {
            "num": t["number"],
            "title": t["label"],
            "stat": t["banner"]["stat"],
            "desc": "",
            "tab_id": t["id"],
        }
        for t in tabs
    ]

    strat_no = len(tabs) + 1
    tabs.append(
        {
            "id": f"t{strat_no}",
            "number": f"{strat_no:02d}",
            "label": "Strategic Intelligence",
            "banner": {
                "tag": f"Trend {strat_no:02d} · Strategic Intelligence",
                "title": f"Where Should {brand or 'the Brand'} Bet?",
                "title_em": "",
                "stat": "",
                "desc": "",
                "image": None,
            },
            "strategy": {
                "title": f"{brand or 'The Brand'}'s Winning Formula",
                "body": "",
                "formula": [],
            },
        }
    )

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window_label": window_label,
            "dataset_mode": dataset_mode,
            "total_articles": total,
            "logos": brand_media.brand_logos([brand, *known], articles),
        },
        "hero": hero,
        "overview": {
            "context": "",
            "cards": cards,
            "whats_next": {"label": "Start exploring", "title": "", "sub": "", "cta": ""},
        },
        "tabs": tabs,
        "footer": {
            "note": f"{total} tagged mentions · {window_label}",
            "brand": "InfoVision / AlphaMetricx · Brand Intelligence",
        },
    }
