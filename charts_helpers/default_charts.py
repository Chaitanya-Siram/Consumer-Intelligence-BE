import heapq
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from .pr_calculation import calculate_pr_score
from db_helpers.schema import ChartResult


class ChartCalulations:
    def __init__(self):
        pass

    def total_count(self, data):
        return ChartResult(
            chart_id="total_count",
            title="Total Count",
            description="Total Count of Articles.",
            chart_type="kpi",
            data={ "total_count": len(data) },
            series=["POS", "NEG", "NEU"],
            x_label="Date",
            y_label="Article Count",
        )
    
    def total_reach(self, data):
        total_reach = 0
        for item in data:
            try:
                total_reach += int(item.get("reach") or 0)
            except (TypeError, ValueError):
                continue
        return ChartResult(
            chart_id="total_reach",
            title="Total Reach",
            description="Total Reach of Articles.",
            chart_type="kpi",
            data={ "total_reach": total_reach },
            series=["POS", "NEG", "NEU"],
            x_label="Date",
            y_label="Article Count",
        )

    def sentiment_distribution(self, data, total_count):
        distribution = {"POS": 0, "NEG": 0, "NEU": 0}
        datewise_dist = {}
        for item in data:
            sentiment = item.get("sentiment")
            date = item.get("date")
            if sentiment in distribution:
                distribution[sentiment] += 1
                if date:
                    date = date[:10]  # Extract YYYY-MM-DD
                    if date not in datewise_dist:
                        datewise_dist[date] = {"POS": 0, "NEG": 0, "NEU": 0}
                    datewise_dist[date][sentiment] += 1

        positive_pct = (distribution["POS"] / total_count) * 100 if total_count > 0 else 0
        negative_pct = (distribution["NEG"] / total_count) * 100 if total_count > 0 else 0
        neutral_pct = (distribution["NEU"] / total_count) * 100 if total_count > 0 else 0
        
        net_sentiment_score = round((distribution["POS"] - distribution["NEG"])/total_count * 100, 2) if total_count > 0 else 0

        # Missing dates in datewise_dist should be filled with 0 counts for all sentiments
        if datewise_dist:
            min_date = min(datewise_dist.keys())
            max_date = max(datewise_dist.keys())
            current_date = datetime.strptime(min_date, "%Y-%m-%d")
            max_date = datetime.strptime(max_date, "%Y-%m-%d")
            while current_date <= max_date:
                date_str = current_date.strftime("%Y-%m-%d")
                if date_str not in datewise_dist:
                    datewise_dist[date_str] = {"POS": 0, "NEG": 0, "NEU": 0}
                current_date += timedelta(days=1)

            # Sort datewise distribution by date
            datewise_dist = dict(sorted(datewise_dist.items()))

        chart_data = {
            "net_sentiment_score": net_sentiment_score,
            "POS": {"count": distribution["POS"], "percentage": round(positive_pct, 2)},
            "NEG": {"count": distribution["NEG"], "percentage": round(negative_pct, 2)},
            "NEU": {"count": distribution["NEU"], "percentage": round(neutral_pct, 2)},
            "datewise_distribution": datewise_dist,
        }

        return ChartResult(
            chart_id="sentiment_distribution",
            title="Sentiment Distribution",
            description="Overall and date-wise breakdown of positive, negative, and neutral coverage, with the net sentiment score.",
            chart_type="donut",
            data=chart_data,
            series=["POS", "NEG", "NEU"],
            x_label="Date",
            y_label="Article Count",
        )
    
    def datewise_coverage(self, data):
        coverage = {}
        for item in data:
            date = item.get("date") or item.get("published_date") or item.get("timestamp") or item.get("pubDate")
            if date:
                date = date[:10]  # Extract YYYY-MM-DD
                if date not in coverage:
                    coverage[date] = 0
                coverage[date] += 1
        
        min_date = min(coverage.keys()) if coverage else None
        max_date = max(coverage.keys()) if coverage else None

        # Fill in missing dates with 0 count
        if min_date and max_date:
            current_date =  datetime.strptime(min_date, "%Y-%m-%d")
            max_date = datetime.strptime(max_date, "%Y-%m-%d")
            while current_date <= max_date:
                if current_date.strftime("%Y-%m-%d") not in coverage:
                    coverage[current_date.strftime("%Y-%m-%d")] = 0
                current_date += timedelta(days=1)

        # Sort coverage by date
        coverage = dict(sorted(coverage.items()))

        result = [{"date": date, "count": count} for date, count in coverage.items()]

        return ChartResult(
            chart_id="datewise_coverage",
            title="Coverage Over Time",
            description="Article volume per day, with missing days zero-filled for a continuous series.",
            chart_type="line",
            data=result,
            series=[],
            x_label="Date",
            y_label="Article Count",
        )

    def theme_distribution(self, data):
        distribution = {}
        sentiment_theme_dist = {}
        for item in data:
            theme = item.get("theme")
            sentiment = item.get("sentiment")
            if theme:
                if theme not in distribution:
                    distribution[theme] = 0
                distribution[theme] += 1
            
                if theme not in sentiment_theme_dist:
                    sentiment_theme_dist[theme] = {"POS": 0, "NEG": 0, "NEU": 0}
                sentiment_theme_dist[theme][sentiment] += 1
        
        # Sort themes by count in descending order
        distribution = dict(sorted(distribution.items(), key=lambda x: x[1], reverse=True))

        result = []
        for theme, count in distribution.items():
            # Capitalize each word in theme for better display
            sentiment_dist = sentiment_theme_dist.get(theme, {"POS": 0, "NEG": 0, "NEU": 0})
            # theme = str(theme).title()
            result.append({"theme": theme, "count": count, "sentiments": sentiment_dist})

        return ChartResult(
            chart_id="theme_distribution",
            title="Theme Distribution",
            description="Top themes by article volume, each with its positive/negative/neutral sentiment split.",
            chart_type="bar",
            data=result[:10],  # Top 10 themes
            series=[],
            x_label="Theme",
            y_label="Article Count",
        )

    def sov_and_competitve_matrix(self, data):
        """
        Calculate share of voice (SOV) for each brand mentioned in the data, including the brand of interest and its competitors.
            - brand_of_interest is expected to be a list, but we will only consider the first entry as the main brand for SOV calculation. This allows for handling synonyms or variations of the brand name.
            - competitors is expected to be a list of competitor brand names.
            - SOV is calculated as the count of mentions for each brand divided by the total mentions of all brands, expressed as a percentage.
            - The function returns the top 10 brands by SOV in descending order.
        Args:
            data (list): A list of records, where each record is expected to be a dictionary containing at least "brand_of_interest" and "competitors" keys.
        Returns:
            list: A list of dictionaries, each containing "brand" and "count" keys, representing the brand name and its corresponding mention count, sorted by count in descending order.
        """

        def _new_stat():
            return {"count": 0, "sentiments": {"POS": 0, "NEG": 0, "NEU": 0}, "reach": 0}

        brand_stats: dict[str, dict] = defaultdict(_new_stat)
        sov_stats: dict[str, dict] = defaultdict(_new_stat)
        competitors_dict = defaultdict(int)

        for item in data:
            sentiment = item.get("sentiment")
            try:
                reach = int(item.get("reach") or 0)
            except (TypeError, ValueError):
                reach = 0

            brand_of_interest_list = item.get("brand_of_interest")
            if brand_of_interest_list:
                stat = brand_stats[brand_of_interest_list[0]]
                stat["count"] += 1
                if sentiment in stat["sentiments"]:
                    stat["sentiments"][sentiment] += 1
                stat["reach"] += reach

            for brand in item.get("competitors") or ():
                stat = sov_stats[brand]
                competitors_dict[brand] += 1
                stat["count"] += 1
                if sentiment in stat["sentiments"]:
                    stat["sentiments"][sentiment] += 1
                stat["reach"] += reach

        # Synonyms of the brand of interest collapse under the most-frequent spelling.
        main_brand = None
        if brand_stats:
            main_brand = max(brand_stats, key=lambda b: brand_stats[b]["count"])
            merged = sov_stats[main_brand]
            for stat in brand_stats.values():
                merged["count"] += stat["count"]
                for s in ("POS", "NEG", "NEU"):
                    merged["sentiments"][s] += stat["sentiments"][s]
                merged["reach"] += stat["reach"]

        # Top 10 by count, descending.
        top = heapq.nlargest(5, sov_stats.items(), key=lambda kv: kv[1]["count"])

        sov, competitive_matrix = [], []
        for brand, stat in top:
            sov.append({
                "brand": brand,
                "count": stat["count"],
            })

            total_mentions = stat["count"]
            net_sentiment = round((stat["sentiments"]["POS"] - stat["sentiments"]["NEG"]) / total_mentions * 100, 2) if total_mentions > 0 else 0

            competitive_matrix.append({
                "brand": brand,
                "total_mentions": total_mentions,
                "net_sentiment": net_sentiment,
                "sentiments": stat["sentiments"],
                "reach": stat["reach"],
            })

        # Get list of top 10 competitors (List[str]) based on mention count
        top_competitors = heapq.nlargest(5, competitors_dict.items(), key=lambda kv: kv[1])

        sov_chart = ChartResult(
            chart_id="share_of_voice",
            title="Share of Voice",
            description="Mention share across the brand of interest and its top competitors.",
            chart_type="bar",
            data=sov,
            series=[],
            x_label="Brand",
            y_label="Mentions",
        )
        competitive_matrix_chart = ChartResult(
            chart_id="competitive_matrix",
            title="Competitive Matrix",
            description="Per-brand total mentions, net sentiment, and reach for the brand and its competitors.",
            chart_type="bubble",
            data=competitive_matrix,
            series=[],
            x_label="Total Mentions",
            y_label="Net Sentiment",
        )

        return sov_chart, competitive_matrix_chart, main_brand, top_competitors

    def publication_tier(self, data):
        """
        Get publication tier using range of reach values
            - T1 if reach >= 10M
            - T2 if reach >= 1M and < 10M
            - T3 if reach >= 100K and < 1M
            - T4 if reach < 100K
        """
        tier_dict = {"T1": 0, "T2": 0, "T3": 0, "T4": 0}

        for article in data:
            if "reach" in article:
                if article["reach"] >= 10_000_000:
                    tier_dict["T1"] += 1
                elif article["reach"] >= 1_000_000:
                    tier_dict["T2"] += 1
                elif article["reach"] >= 100_000:
                    tier_dict["T3"] += 1
                else:
                    tier_dict["T4"] += 1

        return ChartResult(
            chart_id="publication_tier",
            title="Publication Tier Distribution",
            description="Article counts by publication reach tier (T1 ≥ 10M, T2 ≥ 1M, T3 ≥ 100K, T4 < 100K).",
            chart_type="bar",
            data=tier_dict,
            series=[],
            x_label="Tier",
            y_label="Article Count",
        )
    
    def publication_reach_sentiment(self, data):
        """
        Aggregate total reach, net sentiment, and article count per publication.

        Args:
            data (list): Records expected to have "domain", "reach", and "sentiment" keys.
        Returns:
            list[dict]: One entry per publication with {label, total_reach, net_sentiment,
            total_count}, sorted by total_count descending.
        """
        publication_stats: dict[str, dict] = defaultdict(
            lambda: {
                "total_reach": 0,
                "total_count": 0,
                "sentiments": {"POS": 0, "NEG": 0, "NEU": 0},
            }
        )

        for item in data:
            publication = item.get("domain_name")
            if not publication:
                continue

            sentiment = item.get("sentiment")
            try:
                reach = int(item.get("reach") or 0)
            except (TypeError, ValueError):
                reach = 0

            stat = publication_stats[publication]
            stat["total_reach"] += reach
            stat["total_count"] += 1
            if sentiment in stat["sentiments"]:
                stat["sentiments"][sentiment] += 1

        result = []
        for publication, stats in publication_stats.items():
            scored_mentions = sum(stats["sentiments"].values())
            if scored_mentions > 0:
                net_sentiment = round((stats["sentiments"]["POS"] - stats["sentiments"]["NEG"]) / scored_mentions * 100, 2)
            else:
                net_sentiment = 0
            result.append({
                "label": publication,
                "total_reach": stats["total_reach"],
                "net_sentiment": net_sentiment,
                "total_count": stats["total_count"],
            })

        result.sort(key=lambda x: x["total_count"], reverse=True)

        return ChartResult(
            chart_id="publication_reach_sentiment",
            title="Publication Reach vs Sentiment",
            description="Per-publication total reach, net sentiment, and article count.",
            chart_type="scatter",
            data=result,
            series=[],
            x_label="Total Reach",
            y_label="Net Sentiment",
        )

    def top_publications(self, data, limit: int = 10):
        """Top N publications by article count.

        Args:
            data (list): Records expected to have a "domain" key.
            limit (int): Maximum number of publications to return.
        Returns:
            list[dict]: [{"label": <domain>, "count": <int>}, ...] sorted by count desc.
        """
        counts: dict[str, int] = defaultdict(int)
        for item in data:
            publication = item.get("domain_name")
            if publication:
                counts[publication] += 1

        top = heapq.nlargest(limit, counts.items(), key=lambda kv: kv[1])
        result = [{"label": publication, "count": count} for publication, count in top]

        return ChartResult(
            chart_id="top_publications",
            title="Top Publications",
            description="Publications ranked by article count.",
            chart_type="bar",
            data=result,
            series=[],
            x_label="Publication",
            y_label="Article Count",
        )

    def publish_time_heatmap(self, data):
        """7×24 heatmap of article counts by day-of-week × hour-of-day (UTC).

        Args:
            data (list): Records with a date-like field ("date", "published_date",
                "timestamp", or "pubDate") parsable as ISO 8601.
        Returns:
            list[dict]: One entry per day (Sunday..Saturday), each with a 24-entry
            "data" list of {hour, count}. Missing cells are filled with count=0
            so the heatmap grid is always complete.
        """
        days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        counts: dict[tuple[int, int], int] = defaultdict(int)

        for item in data:
            date = (
                item.get("date")
                or item.get("published_date")
                or item.get("timestamp")
                or item.get("pubDate")
            )
            if not date:
                continue
            try:
                # ISO 8601 with 'Z' suffix isn't accepted by fromisoformat until 3.11.
                if isinstance(date, str) and date.endswith("Z"):
                    date = date.replace("Z", "+00:00")
                dt = datetime.fromisoformat(date) if isinstance(date, str) else date
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc)
            except (ValueError, TypeError, AttributeError):
                continue

            # datetime.weekday(): Monday=0 ... Sunday=6 — shift so Sunday=0 to match `days`.
            day_idx = (dt.weekday() + 1) % 7
            counts[(day_idx, dt.hour)] += 1

        result = [
            {
                "day": day_name,
                "data": [
                    {"hour": hour, "count": counts.get((day_idx, hour), 0)}
                    for hour in range(24)
                ],
            }
            for day_idx, day_name in enumerate(days)
        ]

        return ChartResult(
            chart_id="publish_time_heatmap",
            title="Publishing Time Heatmap",
            description="Article counts by day of week and hour of day (UTC), as a 7×24 grid.",
            chart_type="heatmap",
            data=result,
            series=[],
            x_label="Hour of Day",
            y_label="Day of Week",
        )

    def top_authors_by_publications(self, data, limit: int = 10):
        """Top N authors by article count, with their most-frequent publication,
        theme, and dominant sentiment.

        Args:
            data (list): Records with "author" (string or list), "domain", "theme",
                and "sentiment" keys.
            limit (int): Maximum number of authors to return.
        Returns:
            list[dict]: [{author, publication, theme, total_count, sentiment}],
            sorted by total_count descending. `publication`/`theme`/`sentiment`
            are the author's most-frequent values, or None if never recorded.
        """
        def _new_stat():
            return {
                "count": 0,
                "publications": defaultdict(int),
                "themes": defaultdict(int),
                "sentiments": {"POS": 0, "NEG": 0, "NEU": 0},
            }

        author_stats: dict[str, dict] = defaultdict(_new_stat)

        for item in data:
            author_field = item.get("author")
            if not author_field:
                continue

            # Some articles credit multiple authors; accept both list and string.
            if isinstance(author_field, list):
                authors = [a for a in author_field if a]
            else:
                authors = [author_field]
            if not authors:
                continue

            publication = item.get("domain_name")
            theme = item.get("theme")
            sentiment = item.get("sentiment")

            for auth in authors:
                stat = author_stats[auth]
                stat["count"] += 1
                if publication:
                    stat["publications"][publication] += 1
                if theme:
                    stat["themes"][theme] += 1
                if sentiment in stat["sentiments"]:
                    stat["sentiments"][sentiment] += 1

        top = heapq.nlargest(limit, author_stats.items(), key=lambda kv: kv[1]["count"])

        result = []
        for author, stat in top:
            top_pub = (
                max(stat["publications"], key=stat["publications"].get)
                if stat["publications"]
                else None
            )
            top_theme = (
                max(stat["themes"], key=stat["themes"].get)
                if stat["themes"]
                else None
            )
            top_sentiment = (
                max(stat["sentiments"], key=stat["sentiments"].get)
                if any(stat["sentiments"].values())
                else None
            )
            result.append({
                "author": author,
                "publication": top_pub,
                "theme": top_theme,
                "total_count": stat["count"],
                "sentiment": top_sentiment,
            })

        return ChartResult(
            chart_id="top_authors_by_publications",
            title="Top Authors",
            description="Most prolific authors with their most-frequent publication, theme, and dominant sentiment.",
            chart_type="table",
            data=result,
            series=[],
            x_label="",
            y_label="",
        )

    def top_articles_by_sentiment(
        self, data, per_sentiment: int = 6, snippet_length: int = 300
    ):
        """Top articles by reach, partitioned by sentiment.

        Args:
            data (list): Records with "sentiment", "reach", "id", "title",
                "content"/"article_text", "domain", "theme", and a date field.
            per_sentiment (int): How many articles to pick per sentiment bucket.
            snippet_length (int): Max characters of `content` to include.
        Returns:
            dict: {"POS": [...], "NEG": [...], "NEU": [...]} — each list has up
            to `per_sentiment` articles, sorted by reach descending.
        """
        def _reach(item):
            try:
                return int(item.get("reach") or 0)
            except (TypeError, ValueError):
                return 0

        def _snippet(text):
            if not text:
                return ""
            text = str(text).strip()
            return text if len(text) <= snippet_length else text[:snippet_length].rstrip() + "..."

        def _date(item):
            return (
                item.get("date")
                or item.get("published_date")
                or item.get("timestamp")
                or item.get("pubDate")
            )

        buckets: dict[str, list] = {"POS": [], "NEG": [], "NEU": []}
        for item in data:
            sentiment = item.get("sentiment")
            if sentiment in buckets:
                buckets[sentiment].append(item)

        result: dict[str, list[dict]] = {"POS": [], "NEG": [], "NEU": []}
        for sentiment, items in buckets.items():
            top = heapq.nlargest(per_sentiment, items, key=_reach)
            for it in top:
                content = it.get("content") or it.get("article_text") or it.get("description")
                result[sentiment].append({
                    "id": it.get("id"),
                    "title": it.get("title"),
                    "content": _snippet(content),
                    "sentiment": sentiment,
                    "date": _date(it),
                    "theme": it.get("theme"),
                    "domain": it.get("domain_name"),
                })

        return ChartResult(
            chart_id="top_articles_by_sentiment",
            title="Top Articles by Sentiment",
            description="Highest-reach articles in each sentiment bucket (positive, negative, neutral).",
            chart_type="table",
            data=result,
            series=["POS", "NEG", "NEU"],
            x_label="",
            y_label="",
        )

    def original_vs_syndicated(self, data):
        """Classify articles as original vs syndicated based on shared titles.

        Convention: for any title that appears N times, 1 article is counted as
        the original and the remaining N-1 are syndicated copies. Titles are
        compared after stripping whitespace and lower-casing so trivial
        variations don't split syndicated groups apart.

        Args:
            data (list): Records with a "title" key.
        Returns:
            dict: counts and percentages over articles that have a usable title.
            Articles with empty/missing titles are tallied separately under
            "untitled" and excluded from the original/syndicated split.
        """
        title_counts: dict[str, int] = defaultdict(int)
        untitled = 0
        for item in data:
            title = item.get("title")
            if not title or not str(title).strip():
                untitled += 1
                continue
            title_counts[str(title).strip().lower()] += 1

        titled_total = sum(title_counts.values())
        original_count = len(title_counts)
        syndicated_count = titled_total - original_count

        def _pct(n: int) -> float:
            return round((n / titled_total) * 100, 2) if titled_total else 0

        chart_data = {
            "original": {"count": original_count, "percentage": _pct(original_count)},
            "syndicated": {"count": syndicated_count, "percentage": _pct(syndicated_count)},
            "total": titled_total,
            "untitled": untitled,
        }

        return ChartResult(
            chart_id="original_vs_syndicated",
            title="Original vs Syndicated",
            description="Share of original articles versus syndicated copies, grouped by shared (normalized) title.",
            chart_type="donut",
            data=chart_data,
            series=["original", "syndicated"],
            x_label="",
            y_label="",
        )

    def media_types_by_competitors(self, data, competitors):
        """
        Calculate distribution of media types for each competitor brand.

        Args:
            data (list): Records expected to have "competitors" (list of brand names) and "media_type" keys.
        Returns:
            dict: A mapping of competitor brand to a distribution of media types, e.g.:
            {
                "BrandA": {"Online News": 10, "Blog": 5, "Social Media": 2},
                "BrandB": {"Online News": 7, "Blog": 3, "Social Media": 8},
                ...
            }
        """
        distribution: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for item in data:
            media_type = item.get("media_type")
            if not media_type:
                continue

            for brand in item.get("brand_of_interest") or []:
                distribution[brand][media_type] += 1

            article_competitors = item.get("competitors") or []
            if competitors:
                for brand in article_competitors:
                    if brand in competitors:
                        distribution[brand][media_type] += 1
            else:
                for brand in article_competitors:
                    distribution[brand][media_type] += 1

        # Convert defaultdicts to regular dicts for cleaner output
        final_distribution = {brand: dict(media_counts) for brand, media_counts in distribution.items()}

        return ChartResult(
            chart_id="media_types_by_competitors",
            title="Media Types by Brand",
            description="Distribution of media types per brand and competitor.",
            chart_type="stacked_bar",
            data=final_distribution,
            series=[],
            x_label="Brand",
            y_label="Article Count",
        )

    def coverage_overtime_by_competitors(self, data, competitors):
        """ Calculate coverage over time for each competitor brand. """
        coverage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for item in data:
            date = item.get("date")
            if date:
                date = date[:10]  # Extract YYYY-MM-DD
            else:
                continue

            for brand in item.get("brand_of_interest") or []:
                coverage[brand][date] += 1

            article_competitors = item.get("competitors") or []
            if competitors:
                for brand in article_competitors:
                    if brand in competitors:
                        coverage[brand][date] += 1
            else:
                for brand in article_competitors:
                    coverage[brand][date] += 1

        # Keep only the top 5 brands by total coverage count.
        top_brands = heapq.nlargest(
            5, coverage.items(), key=lambda kv: sum(kv[1].values())
        )
        final_coverage = {
            brand: dict(sorted(date_counts.items()))
            for brand, date_counts in top_brands
        }

        return ChartResult(
            chart_id="coverage_overtime_by_competitors",
            title="Coverage Over Time by Brand",
            description="Daily coverage volume per brand and competitor (top 5 by total coverage).",
            chart_type="line",
            data=final_coverage,
            series=[],
            x_label="Date",
            y_label="Article Count",
        )

    def sentiment_breakdown_by_competitors(self, data, competitors):
        """ Calculate sentiment breakdown + net sentiment + total reach for each competitor brand. """
        sentiment_breakdown: dict[str, dict[str, int]] = defaultdict(lambda: {"POS": 0, "NEG": 0, "NEU": 0})
        reach_by_brand: dict[str, int] = defaultdict(int)

        for item in data:
            full_text = item.get("title", "") + " " + item.get("content", "")
            sentiment = item.get("sentiment")
            if sentiment not in ["POS", "NEG", "NEU"]:
                continue

            try:
                reach = int(item.get("reach") or 0)
            except (TypeError, ValueError):
                reach = 0

            for brand in item.get("brand_of_interest") or []:
                sentiment_breakdown[brand][sentiment] += 1
                reach_by_brand[brand] += reach

            article_competitors = item.get("competitors") or []
            if competitors:
                for brand in competitors:
                    if brand in article_competitors or brand in full_text:
                        sentiment_breakdown[brand][sentiment] += 1
                        reach_by_brand[brand] += reach
            else:
                for brand in article_competitors:
                    sentiment_breakdown[brand][sentiment] += 1
                    reach_by_brand[brand] += reach

        # Keep only the top 5 brands by total mention count.
        top_brands = heapq.nlargest(
            5, sentiment_breakdown.items(), key=lambda kv: sum(kv[1].values())
        )
        final_breakdown: dict = {}
        for brand, sentiments in top_brands:
            total = sentiments["POS"] + sentiments["NEG"] + sentiments["NEU"]
            net_sentiment = round((sentiments["POS"] - sentiments["NEG"]) / total * 100, 2) if total > 0 else 0
            final_breakdown[brand] = {
                "total_mentions": total,
                "net_sentiment": net_sentiment,
                "total_reach": reach_by_brand[brand],
                "sentiments": dict(sentiments),
            }

        return ChartResult(
            chart_id="sentiment_breakdown_by_competitors",
            title="Sentiment Breakdown by Brand",
            description="Per-brand sentiment split, net sentiment, and total reach (top 5 by mentions).",
            chart_type="stacked_bar",
            data=final_breakdown,
            series=["POS", "NEG", "NEU"],
            x_label="Brand",
            y_label="Mentions",
        )

    def message_consistency_by_competitors(self, data, brands, competitors, elements):
        """ Calculate message consistency for each competitor brand based on presence of key messages in article content. """
        all_brands = set(brands) | set(competitors)

        final_response = []
        final_datewise_response = []
        for brand in all_brands:
            brand_data = {"total": 0, "actual_score": 0, "sentiments": {"POS": 0, "NEG": 0, "NEU": 0}, "reach": 0}
            datewise_data = defaultdict(lambda: {"total": 0, "actual_score": 0})
            for article in data:
                full_text = article.get("title", "") + " " + article.get("content", "")
                if brand in article.get("brand_of_interest", []) or brand in article.get("competitors", []) or brand in full_text:
                    brand_data["total"] += 1
                    date = article.get("date")[:10]
                    datewise_data[date]["total"] += 1
                    if any(element in full_text for element in elements):
                        brand_data["actual_score"] += 1
                        datewise_data[date]["actual_score"] += 1
                    sentiment = article.get("sentiment")
                    brand_data["sentiments"][sentiment] += 1
                    brand_data["reach"] += int(article.get("reach") or 0)
            
            expected_score = len(elements) * brand_data["total"]
            actual_score = brand_data["actual_score"]
            consistency_percent = round((actual_score / expected_score) * 100, 2) if expected_score > 0 else 0
            positives = brand_data["sentiments"]["POS"]
            negatives = brand_data["sentiments"]["NEG"]
            total_count = brand_data["total"]

            net_sentiment = round((positives - negatives) / total_count * 100, 2) if total_count > 0 else 0
            
            final_response.append({
                "brand": brand,
                "total_articles": len(data),
                "total_elements": len(elements),
                "expected_score": expected_score,
                "actual_score": actual_score,
                "consistency_percent": consistency_percent,
                "reach": brand_data["reach"],
                "positive": positives,
                "negative": negatives,
                "neutral": brand_data["sentiments"]["NEU"],
                "net_sentiment": net_sentiment,
            })

            datewise_scores = []
            for date, scores in datewise_data.items():
                expected_score = len(elements) * scores["total"]
                actual_score = scores["actual_score"]
                consistency_percent = round((actual_score / expected_score) * 100, 2) if expected_score > 0 else 0
                datewise_scores.append({
                    "date": date,
                    "total_articles": scores["total"],
                    "total_elements": len(elements),
                    "expected_score": expected_score,
                    "actual_score": actual_score,
                    "consistency_percent": consistency_percent,
                })

            final_datewise_response.append({
                "brand": brand,
                "data": sorted(datewise_scores, key=lambda x: x["date"])
            })

        consistency_chart = ChartResult(
            chart_id="message_consistency",
            title="Message Consistency",
            description="How consistently each brand's coverage carries the key message elements.",
            chart_type="bar",
            data=final_response,
            series=[],
            x_label="Brand",
            y_label="Consistency (%)",
        )
        datewise_consistency_chart = ChartResult(
            chart_id="coverage_message_consistency",
            title="Message Consistency Over Time",
            description="Daily message-consistency score per brand.",
            chart_type="line",
            data=final_datewise_response,
            series=[],
            x_label="Date",
            y_label="Consistency (%)",
        )

        return consistency_chart, datewise_consistency_chart

    def publication_by_brands_and_competitors(self, data, brands, competitors):
        """ Calculate publication distribution for each brand and competitor. """
        all_brands = set(brands) | set(competitors)
        distribution: dict[str, dict[str, int]] = {brand: defaultdict(int) for brand in all_brands}

        for item in data:
            full_text = item.get("title", "") + " " + item.get("content", "")
            publication = item.get("domain_name")
            if not publication:
                continue
            for brand in all_brands:
                if brand in item.get("brand_of_interest", []) or brand in item.get("competitors", []) or brand in full_text:
                        distribution[brand][publication] += 1

        final_distribution = {brand: dict(pub_counts) for brand, pub_counts in distribution.items()}

        # Top 5 publications per brand
        for brand, pub_counts in final_distribution.items():
            top_pubs = heapq.nlargest(5, pub_counts.items(), key=lambda kv: kv[1])
            final_distribution[brand] = dict(top_pubs)

        return ChartResult(
            chart_id="publication_by_brands_and_competitors",
            title="Top Publications by Brand",
            description="Top 5 publications covering each brand and competitor.",
            chart_type="bar",
            data=final_distribution,
            series=[],
            x_label="Publication",
            y_label="Article Count",
        )
    
    # ==============================================================================
    # Dashboard chart functions ====================================================
    # ==============================================================================
    def media_intelligence_charts(self, data):
        total_count = self.total_count(data)

        return {
            "total_count": total_count,
            "total_reach": self.total_reach(data),
            "sentiment_distribution": self.sentiment_distribution(data, total_count),
            "datewise_coverage": self.datewise_coverage(data),
            "syndication": self.original_vs_syndicated(data),
            "theme_distribution": self.theme_distribution(data),
            "top_publications": self.top_publications(data),
            "publication_reach_sentiment": self.publication_reach_sentiment(data),
            "publish_time_heatmap": self.publish_time_heatmap(data),
            "top_authors": self.top_authors_by_publications(data),
            "top_articles": self.top_articles_by_sentiment(data),
        }

    def pr_impact_charts(self, data, competitors):
        total_count = self.total_count(data)
        sov, competitive_matrix, main_brand, top_competitors = self.sov_and_competitve_matrix(data)
        sentiment_trend = self.sentiment_distribution(data, total_count)
        coverage_volume = self.datewise_coverage(data)
        publication_tier = self.publication_tier(data)
        pr_impact = calculate_pr_score(data, main_brand, weightage=None)
        if competitors:
            pr_impact_competitors = [calculate_pr_score(data, cp, weightage=None) for cp in competitors]
        else:
            pr_impact_competitors = [calculate_pr_score(data, cp, weightage=None) for cp, _ in top_competitors]

        return {
            "total_count": total_count,
            "total_reach": self.total_reach(data),
            "share_of_voice": sov,
            "sentiment_distribution": sentiment_trend,
            "competitive_matrix": competitive_matrix,
            "coverage_volume": coverage_volume,
            "publication_tier": publication_tier,
            "pr_impact": pr_impact,
            "pr_impact_competitors": pr_impact_competitors,
        }

    def narrative_intelligence_charts(self, data, brand_keywords, competitors, message_keywords):
        
        consistency, datewise_consistency = self.message_consistency_by_competitors(data, brand_keywords, competitors, message_keywords)

        return {
            "total_count": self.total_count(data),
            "total_reach": self.total_reach(data),
            "media_types": self.media_types_by_competitors(data, competitors),
            "coverage_overtime": self.coverage_overtime_by_competitors(data, competitors),
            "sentiment_breakdown": self.sentiment_breakdown_by_competitors(data, competitors),
            "message_consistency": consistency,
            "coverage_message_consistency": datewise_consistency,
            "top_publications": self.publication_by_brands_and_competitors(data, brand_keywords, competitors),
        }

chart_calculations = ChartCalulations()