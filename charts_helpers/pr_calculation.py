from collections import defaultdict
from datetime import datetime, timedelta, timezone

from db_helpers.schema import ChartResult


def count_keyword_occurrences(text, keyword):
    """
    Count the occurrences of a keyword in a given text.
    """
    return text.lower().count(keyword.lower())


HIGH_LEVEL_ROLES = ["CEO", "CTO", "CFO", "VICE PRESIDENT", "DIRECTOR"]
MID_LEVEL_ROLES = [
    "SPOKESPERSON", "SPOKESWOMEN", "AMBASSADOR",
    "COMPANY SPOKESPERSON", "PR COMMUNICATION",
    "ANALYST", "SPECIALIST", "CONSULTANT",
    "RESEARCHER", "ASSOCIATE", "SCIENTIST"
]

def calculate_gauge_and_rating(sum_pr_impact, no_days):
    gauge = round((sum_pr_impact / no_days), 2) if no_days > 0 else 0
    if gauge < 0:
        rating_scale = "Very Poor"
    elif 0 <= gauge <= 15:
        rating_scale = "Poor"
    elif 16 <= gauge <= 30:
        rating_scale = "Good"
    elif 31 <= gauge <= 45:
        rating_scale = "Very Good"
    else:
        rating_scale = "Excellent"
    return gauge, rating_scale


def calculate_pr_score(data, brand_keyword, weightage: None, chart_id="pr_impact"):
    """
    Calculate the PR score based on the provided CSV file, brand keyword, and weightage.
    The function reads the CSV file, processes the data, and calculates the PR impact score based on various factors such as keyword mentions, reach, spokesperson role, and sentiment. The final results include the total count of records, sum of PR impact, gauge, and rating scale.

    :param csv: The CSV file containing the data to be processed.
    :param brand_keyword: The brand keyword to be used for calculating the PR score.
    :param weightage: The weightage for different factors (optional).
    :return: A dictionary containing the calculated PR score and related metrics.
    """

    total_count = len(data)
    sum_pr_impact = 0
    results = defaultdict(lambda: {
        "doc_count": 0,
        "pr_impact": 0,
        "authors_sentiment": {
            "POS": 0,
            "NEG": 0,
            "NEU": 0
        }
    })

    # Calculate PR score
    for record in data:
        if not record.get("title") and not record.get("content"):
            continue

        combined_text = record["title"] or "" + " " + record["content"] or ""
        if not combined_text.strip():
            continue

        brand_mentions = count_keyword_occurrences(combined_text, brand_keyword)

        keyword_score = 0
        if brand_mentions >= 5:
            keyword_score = 5
        elif brand_mentions >= 3:
            keyword_score = 3
        elif brand_mentions >= 1:
            keyword_score = 1
        else:
            keyword_score = 0
        
        # Calculate reach weight
        reach_weight = 0
        reach_value = record.get("reach", 0)
        if 0 <= reach_value <= 100000:
            reach_weight = 1
        elif 100000 < reach_value <= 500000:
            reach_weight = 2
        elif 500000 < reach_value <= 800000:
            reach_weight = 3
        elif 800000 < reach_value <= 1000000:
            reach_weight = 4
        elif reach_value > 1000000:
            reach_weight = 5

        # Calculate spokesperson score
        spokesperson_score = 1
        if any(role in combined_text for role in HIGH_LEVEL_ROLES):
            spokesperson_score = 5
        elif any(role in combined_text for role in MID_LEVEL_ROLES):
            spokesperson_score = 3

        # Calculate PR impact
        if weightage is None:
            # Filler for Article Strength 10% of 1 point
            article_pr_impact = (0.4 * keyword_score) + (0.3 * reach_weight) + (0.2 * spokesperson_score) + 0.1 
        else:
            prominence = int(weightage.get("prominence", 40))
            reach = int(weightage.get("reach", 30))
            spokes_person = int(weightage.get("spokes_person", 20))
            article_strength = int(weightage.get("article_strength", 10))

            article_pr_impact = (prominence/100 * keyword_score) + (reach/100 * reach_weight) + (spokes_person/100 * spokesperson_score) + (article_strength/100)

        # Apply sentiment multiplier
        # For NEU, we multiply by 1, which doesn't change the value
        sentiment = record.get("sentiment") or record.get("articleSentiment") or "NEU"
        if sentiment == "POS":
            article_pr_impact *= 5
        elif sentiment == "NEG":
            article_pr_impact *= -5

        # Divide by 23 and round up to 2 decimal places
        pr_impact = round(article_pr_impact / 23, 2)

        date = record["date"]

        article_date = None
        try:
            if date.endswith("Z"):
                date = date.replace("Z", "+00:00")
            
            article_datetime = datetime.fromisoformat(date)
            article_datetime = article_datetime.astimezone(timezone.utc)
            article_date = article_datetime.date().isoformat()
        except ValueError:
            continue
        
        results[article_date]["doc_count"] += 1
        results[article_date]["pr_impact"] += pr_impact
        results[article_date]["authors_sentiment"][sentiment] += 1

        sum_pr_impact += pr_impact

    # Walk every day between the earliest and latest article date so the
    # chart series has no gaps; missing days get zeroed metrics.
    final_results = []
    if results:
        sorted_dates = sorted(results.keys())
        current = datetime.fromisoformat(sorted_dates[0]).date()
        end = datetime.fromisoformat(sorted_dates[-1]).date()
        while current <= end:
            label = current.isoformat()
            metrics = results.get(label)
            if metrics:
                final_results.append({
                    "label": label,
                    "doc_count": metrics["doc_count"],
                    "pr_impact": round(metrics["pr_impact"], 2),
                    "authors_sentiment": metrics["authors_sentiment"],
                })
            else:
                final_results.append({
                    "label": label,
                    "doc_count": 0,
                    "pr_impact": 0,
                    "authors_sentiment": {"POS": 0, "NEG": 0, "NEU": 0},
                })
            current += timedelta(days=1)

    sorterd_final_results = final_results  # already in chronological order

    no_of_days = len(results)
    gauge, rating_scale = calculate_gauge_and_rating(sum_pr_impact, no_of_days)

    chart_data = {
        "brand_name": brand_keyword,
        "total_count": total_count,
        "gauge": gauge,
        "sum_pr_impact": round(sum_pr_impact, 2),
        "rating_scale": rating_scale,
        "data": sorterd_final_results
    }
    return ChartResult(
        chart_id=chart_id,
        title="PR Score Over Time",
        description=(
            f"Daily PR impact and article volume for '{brand_keyword}', with the "
            "overall gauge (average daily PR impact) and rating scale."
        ),
        chart_type="bar",
        data=chart_data,
        series=["pr_impact", "doc_count"],
        x_label="Date",
        y_label="PR Impact",
    )