from collections import defaultdict
from db_helpers.schema import ChartResult

PER_SECTION_LIMIT = 20


def apply_section_order(result: dict, sections_orders) -> dict:
    """Reorder a {section: articles} map by the given ordered section names: listed
    sections first (empty list if a listed section is absent), then any leftover
    sections in their original order. Matching is case-insensitive."""
    if not sections_orders:
        return result
    by_lower = {k.lower(): k for k in result}
    ordered: dict = {}
    used: set = set()
    for name in sections_orders:
        key = by_lower.get(str(name).strip().lower())
        if key is not None:
            ordered[key] = result[key]
            used.add(key)
        else:
            ordered[str(name)] = []  # listed section with no articles
    for key, articles in result.items():
        if key not in used:
            ordered[key] = articles  # section in data but not in the list
    return ordered


def media_monitoring_charts(data, sections_orders=None):
    """
    Return the top articles per media-monitoring section for the dashboard:
    up to 20 articles for every section present in the data.

    Within each section, priority_watch=True articles come first, then the
    remainder is ordered by reach descending. Sections are taken directly from
    each article's `section` label — no section names are hardcoded.

    When `sections_orders` (the project's ordered section names) is provided, the
    response sections follow that order: listed sections come first (in order, with
    an empty list when a section has no articles), then any leftover sections found
    in the data are appended at the end.
    """

    def _reach_int(item) -> int:
        try:
            return int(item.get("reach") or 0)
        except (TypeError, ValueError):
            return 0

    # Map each main article id -> {domain: url} for its similar articles, so the
    # feed can show "Similar Articles: domain1, domain2" beneath each main.
    similar_map: dict[str, dict[str, str]] = defaultdict(dict)
    for item in data:
        main_id = item.get("similar_of")
        if main_id:
            domain = item.get("domain_name") or item.get("url") or ""
            if domain:
                similar_map[main_id][domain] = item.get("url") or ""

    sections_data: dict[str, list[dict]] = defaultdict(list)
    for item in data:
        # Only main articles in the feed — skip similar and syndicated copies.
        if item.get("similar_of") or item.get("syndication_of"):
            continue
        section = item.get("section") or "Uncategorized"
        sections_data[section].append({
            "id": item.get("id"),
            "title": item.get("title"),
            "content": item.get("content"),
            "summary": item.get("summary"),
            "sentiment": item.get("sentiment"),
            "date": item.get("date"),
            "url": item.get("url"),
            "domain": item.get("domain_name"),
            "author": item.get("author"),
            "reach": item.get("reach"),
            "priority": item.get("priority_watch"),
            "similar_articles": dict(similar_map.get(item.get("id"), {})),
        })

    # Top N per section (priority-watch first, then by reach desc), preserving
    # the order sections first appear in the data.
    result: dict[str, list[dict]] = {}
    for section, articles in sections_data.items():
        articles.sort(
            key=lambda a: (0 if a.get("priority") else 1, -_reach_int(a))
        )
        result[section] = articles[:PER_SECTION_LIMIT]

    # Order sections by the project's sections_orders when provided.
    result = apply_section_order(result, sections_orders)

    # Regroup the final selected articles by date (chronological). Each
    # entry carries its `section` so the frontend can render the day-wise
    # feed without losing context.
    date_grouped: dict[str, list[dict]] = {}
    for section, articles in result.items():
        for article in articles:
            date_key = str(article.get("date") or "")[:10] or "unknown"
            date_grouped.setdefault(date_key, []).append({**article, "section": section})

    grouped_articles = dict(sorted(date_grouped.items()))

    charts_data = ChartResult(
        chart_id="section_articles",
        title="Top Articles by Section",
        description="Top 20 articles per media-monitoring section, with priority-watch items first, then ordered by reach.",
        chart_type="table",
        data=result,
        series=[],
        x_label="",
        y_label="",
    )

    return charts_data, grouped_articles
