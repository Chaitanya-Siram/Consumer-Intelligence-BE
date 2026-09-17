"""Article cohorts shared by Consumer Intelligence lenses.

Pure functions over the normalised tagged-article dicts (sentiment already
mapped to Positive/Negative/Neutral by builder.normalize_articles).

Cohorts:
- brand / competitor / industry splits via aggregate.brands_in
- single vs multiple brand mentions (the platform's honest stand-in for the
  deck's "single vs multiple product holders")
- platform (from the tagger's `source name`, falling back to section/domain)
- sentiment split that always sums to 100 after rounding
"""

from collections import Counter

from . import aggregate

TONE = {"Positive": "pos", "Negative": "neg", "Neutral": "neu"}


def brand_articles(articles: list[dict], brand: str, known: list[str]) -> list[dict]:
    b = (brand or "").lower()
    return [a for a in articles if b and b in {n.lower() for n in aggregate.brands_in(a, known)}]


def competitor_articles(articles: list[dict], brand: str, known: list[str]) -> dict[str, list[dict]]:
    """{competitor: articles} for every known brand other than the subject."""
    out: dict[str, list[dict]] = {}
    b = (brand or "").lower()
    for a in articles:
        for name in aggregate.brands_in(a, known):
            if name.lower() != b:
                out.setdefault(name, []).append(a)
    return out


def top_competitor(articles: list[dict], brand: str, known: list[str]) -> tuple[str | None, list[dict]]:
    comps = competitor_articles(articles, brand, known)
    if not comps:
        return None, []
    name = max(comps, key=lambda n: (len(comps[n]), n))
    return name, comps[name]


def brand_count(article: dict, known: list[str]) -> int:
    return len(aggregate.brands_in(article, known))


def single_multiple(articles: list[dict], known: list[str]) -> tuple[list[dict], list[dict]]:
    """Articles naming exactly one known brand vs two or more. Articles naming
    none are excluded from both (they carry no holder signal)."""
    single = [a for a in articles if brand_count(a, known) == 1]
    multiple = [a for a in articles if brand_count(a, known) >= 2]
    return single, multiple


def platform(article: dict) -> str:
    for field in ("source name", "source_name", "source_type", "platform"):
        v = article.get(field)
        if v and not aggregate.is_junk(v):
            return str(v).strip()
    return aggregate.source_platform(article)


def platforms(articles: list[dict], limit: int = 6) -> list[str]:
    counts = Counter(platform(a) for a in articles)
    return [name for name, _ in counts.most_common(limit)]


def rated(articles: list[dict]) -> list[dict]:
    return [a for a in articles if a.get("sentiment") in TONE]


def pct_rows(counts: dict[str, int], *, order: list[str] | None = None) -> list[dict]:
    """[{"name", "pct", "count"}] with integer pcts that sum to exactly 100
    (largest-remainder rounding). Empty input -> []."""
    names = order or sorted(counts, key=lambda k: (-counts[k], k))
    total = sum(counts.get(n, 0) for n in names)
    if not total:
        return []
    raw = [(n, counts.get(n, 0) * 100 / total) for n in names]
    floors = [(n, int(v), v - int(v)) for n, v in raw]
    short = 100 - sum(f for _, f, _ in floors)
    bump = {n for n, _, _ in sorted(floors, key=lambda t: -t[2])[:short]}
    return [{"name": n, "pct": f + (1 if n in bump else 0), "count": counts.get(n, 0)} for n, f, _ in floors]


def sentiment_split(articles: list[dict]) -> list[dict]:
    """[{"name": "Positive", "pct": 52, "tone": "pos"}, ...] over rated rows,
    summing to 100. Returns [] when nothing is rated."""
    counts = Counter(a["sentiment"] for a in rated(articles))
    rows = pct_rows(dict(counts), order=["Positive", "Negative", "Neutral"])
    return [{"name": r["name"], "pct": r["pct"], "tone": TONE[r["name"]], "count": r["count"]} for r in rows]


def net_sentiment(articles: list[dict]) -> float | None:
    """(positive - negative) / rated, in points -100..100. None when unrated."""
    rows = rated(articles)
    if not rows:
        return None
    pos = sum(1 for a in rows if a["sentiment"] == "Positive")
    neg = sum(1 for a in rows if a["sentiment"] == "Negative")
    return round((pos - neg) * 100 / len(rows), 1)


def negative_share(articles: list[dict]) -> float:
    rows = rated(articles)
    if not rows:
        return 0.0
    return sum(1 for a in rows if a["sentiment"] == "Negative") / len(rows)


def fmt_int(n: float | int) -> str:
    return f"{int(round(n or 0)):,}"
