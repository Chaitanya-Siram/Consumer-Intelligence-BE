"""Time bucketing and change detection over tagged articles.

Shared by every Consumer Intelligence lens that draws a trendline or compares
periods. Pure functions, no I/O. All dates go through `aggregate.parse_day`,
so undated rows are simply dropped from series (never fabricated).

Bucketing is adaptive: datasets shorter than `WEEKLY_UNDER_DAYS` are bucketed
by ISO week, otherwise by calendar month. Two monthly points is not a
trendline, and the platform's capture windows are often 4-6 weeks.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from . import aggregate

WEEKLY_UNDER_DAYS = 90
SPIKE_RATIO = 1.6           # value > ratio x trailing mean -> spike
SPIKE_TRAILING = 4          # buckets in the trailing mean
MAX_SPIKES = 6
MAX_ANNOTATIONS = 3


def _to_date(day: str) -> date:
    return datetime.strptime(day, "%Y-%m-%d").date()


def dated(articles: list[dict]) -> list[tuple[date, dict]]:
    """(date, article) pairs for every article with a parseable date."""
    out = []
    for a in articles:
        day = aggregate.parse_day(a.get("date"))
        if day:
            out.append((_to_date(day), a))
    return out


def window(articles: list[dict]) -> tuple[date | None, date | None]:
    days = [d for d, _ in dated(articles)]
    return (min(days), max(days)) if days else (None, None)


def window_label(articles: list[dict]) -> str:
    """"Nov 22 – Dec 31, 2023" or "Jan – Jun 2024" style label."""
    start, end = window(articles)
    if not start or not end:
        return "Capture window"
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
        return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %Y')} – {end.strftime('%b %Y')}"


def granularity(articles: list[dict]) -> str:
    """'week' for short windows, 'month' otherwise."""
    start, end = window(articles)
    if not start or not end:
        return "month"
    return "week" if (end - start).days < WEEKLY_UNDER_DAYS else "month"


def _bucket_key(day: date, grain: str) -> str:
    if grain == "week":
        monday = day - timedelta(days=day.weekday())
        return monday.isoformat()
    return day.strftime("%Y-%m")


def bucket_label(key: str, grain: str) -> str:
    """Week -> "Nov 27"; month -> "Nov '23"."""
    if grain == "week":
        return _to_date(key).strftime("%b %d").replace(" 0", " ")
    return datetime.strptime(key, "%Y-%m").strftime("%b '%y")


def _fill_gaps(keys: list[str], grain: str) -> list[str]:
    """Every bucket between first and last, so flat weeks show as zeros."""
    if not keys:
        return []
    out = []
    if grain == "week":
        cur, last = _to_date(keys[0]), _to_date(keys[-1])
        while cur <= last:
            out.append(cur.isoformat())
            cur += timedelta(days=7)
    else:
        cur, last = datetime.strptime(keys[0], "%Y-%m"), datetime.strptime(keys[-1], "%Y-%m")
        while cur <= last:
            out.append(cur.strftime("%Y-%m"))
            cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
    return out


def buckets(articles: list[dict], grain: str | None = None, *, keys: list[str] | None = None) -> tuple[list[str], dict[str, list[dict]], str]:
    """Ordered bucket keys (gaps filled), articles per bucket, grain used.

    Pass `keys` (from a wider article set) to bucket a subset over the whole
    capture window, so a theme that only appears in one week still draws a
    full-width series with zeros elsewhere."""
    grain = grain or granularity(articles)
    groups: dict[str, list[dict]] = defaultdict(list)
    for day, a in dated(articles):
        groups[_bucket_key(day, grain)].append(a)
    if keys is None:
        keys = _fill_gaps(sorted(groups), grain)
    else:
        keys = _fill_gaps(sorted(set(keys) | set(groups)), grain)
    return keys, {k: groups.get(k, []) for k in keys}, grain


def count_series(articles: list[dict], grain: str | None = None, *, keys: list[str] | None = None) -> tuple[list[dict], str]:
    """[{"x": label, "y": count, "key": bucket_key}], grain."""
    keys, groups, grain = buckets(articles, grain, keys=keys)
    return [{"x": bucket_label(k, grain), "y": len(groups[k]), "key": k} for k in keys], grain


def spread(articles: list[dict], grain: str | None = None) -> int:
    """Number of distinct buckets the articles fall into."""
    grain = grain or granularity(articles)
    return len({_bucket_key(d, grain) for d, _ in dated(articles)})


def value_series(articles: list[dict], fn, grain: str | None = None) -> tuple[list[dict], str]:
    """Same shape as count_series, with y = fn(bucket_articles). Buckets with
    no articles get y=None so the caller can decide whether to draw them."""
    keys, groups, grain = buckets(articles, grain)
    out = []
    for k in keys:
        rows = groups[k]
        out.append({"x": bucket_label(k, grain), "y": fn(rows) if rows else None, "key": k})
    return out, grain


def peak(points: list[dict]) -> dict | None:
    live = [p for p in points if p.get("y") is not None]
    return max(live, key=lambda p: p["y"]) if live else None


def trough(points: list[dict]) -> dict | None:
    live = [p for p in points if p.get("y") is not None]
    return min(live, key=lambda p: p["y"]) if live else None


def spikes(points: list[dict], *, ratio: float = SPIKE_RATIO, trailing: int = SPIKE_TRAILING, limit: int = MAX_SPIKES) -> list[dict]:
    """Indices where y exceeds `ratio` x the trailing mean of the previous
    `trailing` non-null points (at least one). Returns [{"at": i, "ratio": r}]."""
    out = []
    for i, p in enumerate(points):
        y = p.get("y")
        if y is None or i == 0:
            continue
        prev = [q["y"] for q in points[max(0, i - trailing):i] if q.get("y") is not None]
        if not prev:
            continue
        mean = sum(prev) / len(prev)
        if mean > 0 and y > ratio * mean:
            out.append({"at": i, "ratio": round(y / mean, 2)})
    out.sort(key=lambda s: -s["ratio"])
    return sorted(out[:limit], key=lambda s: s["at"])


def annotations_at(points: list[dict], limit: int = MAX_ANNOTATIONS) -> list[dict]:
    """Peak, trough and the strongest spike as annotation anchors
    [{"at": i, "kind": "peak"|"trough"|"spike"}], de-duplicated by index."""
    picks: list[dict] = []
    pk, tr = peak(points), trough(points)
    if pk:
        picks.append({"at": points.index(pk), "kind": "peak"})
    for s in spikes(points, limit=2):
        picks.append({"at": s["at"], "kind": "spike"})
    if tr and len(points) > 2:
        picks.append({"at": points.index(tr), "kind": "trough"})
    seen, out = set(), []
    for p in picks:
        if p["at"] not in seen:
            seen.add(p["at"])
            out.append(p)
    return sorted(out[:limit], key=lambda p: p["at"])


def halves(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split dated articles into an early and a late half by the median day."""
    rows = sorted(dated(articles), key=lambda t: t[0])
    if len(rows) < 2:
        return [a for _, a in rows], []
    pivot = rows[len(rows) // 2][0]
    early = [a for d, a in rows if d < pivot]
    late = [a for d, a in rows if d >= pivot]
    if not early:  # every row on one day
        return [a for _, a in rows], []
    return early, late


def growth_pct(early: int, late: int, *, cap: int = 300) -> int | None:
    if not early:
        return None
    delta = round((late - early) * 100 / early)
    return max(-cap, min(cap, delta))


def period_pair(articles: list[dict]) -> tuple[list[dict], list[dict], str]:
    """(prior, current, label) for "vs last month" style deltas.

    Uses the last two calendar months when the window spans at least two full
    months, otherwise the two halves of the window with label "vs prior period".
    """
    keys, groups, _ = buckets(articles, "month")
    full = [k for k in keys if groups[k]]
    if len(full) >= 2:
        return groups[full[-2]], groups[full[-1]], "vs last month"
    early, late = halves(articles)
    if not late:
        return [], early, "no prior period"
    return early, late, "vs prior period"


def fmt_delta(value: float | None, *, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "no data"
    rounded = round(value, digits)
    if digits == 0:
        rounded = int(rounded)
    sign = "+" if rounded > 0 else ""
    return f"{sign}{rounded}{unit}"
