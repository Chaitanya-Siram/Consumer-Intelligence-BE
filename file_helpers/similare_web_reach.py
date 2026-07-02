import threading
from io import BytesIO, StringIO
import boto3
import pandas as pd
from datetime import datetime, timedelta
from configs import envs, logger
import requests


def _s3_client():
    return boto3.client(
        's3',
        aws_access_key_id=envs.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=envs.AWS_SECRET_ACCESS_KEY,
        region_name=envs.AWS_REGION
    )


def fetch_reach_from_s3_file() -> dict:
    """ Get reach dict from S3 Current Reach file """

    s3 = _s3_client()
    response = s3.get_object(Bucket=envs.AWS_S3_REACH_BUCKET, Key=envs.AWS_S3_REACH_FILE)
    df = pd.read_csv(BytesIO(response['Body'].read()))
    reach_dict = dict(zip(df['domain'], df['all_traffic_visits']))
    logger.info(f"Loaded reach data for {len(reach_dict)} domains from S3")
    return reach_dict


def fetch_similarweb_reach(source_domain):
    """ Fetch Reach from SimilarWeb """
    total_deduplicated_audience = 0
    try:
        # API base URL
        base_url = f"https://api.similarweb.com/v1/website/{source_domain}/dedup/deduplicated-audiences"

        # Calculate the first day of the current month
        current_date = datetime.today()
        first_day_of_month = datetime(current_date.year, current_date.month, 1)
        last_day_of_previous_month = first_day_of_month - timedelta(days=1)
        previous_month_str = last_day_of_previous_month.strftime("%Y-%m")

        # Query parameters
        params = {
            'api_key': envs.SIMILAR_WEB_REST_API_KEY,
            'start_date': previous_month_str,
            'end_date': previous_month_str,
            'country': 'world',
            'main_domain_only': 'false',
            'format': 'json'
        }

        # Headers
        headers = {
            'accept': 'application/json'
        }

        # Make the GET request
        response = requests.get(base_url, headers=headers, params=params)

        if response.status_code != 200:
            return 0

        json_response = response.json()

        if 'data' in json_response and len(json_response.get('data', [])):
            total_deduplicated_audience = json_response['data'][0]['dedup_data']['total_deduplicated_audience']
        return total_deduplicated_audience
    except Exception as e:
        logger.info(f"Error fetching reach from SimilarWeb: {e}")
        return total_deduplicated_audience


def _persist_new_reach_to_s3(new_reach: dict[str, int]) -> None:
    """Append newly fetched domain→reach rows to the S3 reach CSV in one write.

    Re-reads the current file first so domains added by a concurrent run aren't
    clobbered; only domains still missing from the file are appended. Runs on a
    background thread — any failure is logged and swallowed so it never affects
    the live reach lookup.
    """
    if not new_reach:
        return
    try:
        s3 = _s3_client()
        response = s3.get_object(Bucket=envs.AWS_S3_REACH_BUCKET, Key=envs.AWS_S3_REACH_FILE)
        df = pd.read_csv(BytesIO(response['Body'].read()))

        existing = set(df['domain'].astype(str))
        rows = [
            {'domain': domain, 'all_traffic_visits': int(reach)}
            for domain, reach in new_reach.items()
            if domain and str(domain) not in existing
        ]
        if not rows:
            logger.info("Reach file already contains all fetched domains; nothing to append.")
            return

        updated = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
        buffer = StringIO()
        updated.to_csv(buffer, index=False)
        s3.put_object(
            Bucket=envs.AWS_S3_REACH_BUCKET,
            Key=envs.AWS_S3_REACH_FILE,
            Body=buffer.getvalue().encode('utf-8'),
        )
        logger.info(f"Appended {len(rows)} new domain(s) to the S3 reach file.")
    except Exception as e:
        logger.exception(f"Failed to persist new reach values to S3: {e}")


def update_reach_file_in_background(new_reach: dict[str, int]) -> None:
    """Fire-and-forget the S3 reach-file update on a daemon thread so the caller
    (the live reach lookup) returns immediately and is unaffected by the upload."""
    if not new_reach:
        return
    thread = threading.Thread(
        target=_persist_new_reach_to_s3,
        args=(dict(new_reach),),  # snapshot so later mutations don't leak in
        name="reach-file-updater",
        daemon=True,
    )
    thread.start()


def get_reach(articles: list[dict[str, str]]) -> list[dict]:
    """
    Get reach for a list of articles, using the S3 file first and SimilarWeb API
    as fallback. Reaches fetched from the API for domains missing from the S3
    file are collected and written back to the file in ONE go, on a background
    thread, so this function returns without waiting on the upload.

    Args:
        articles: list of article dicts, each containing at least a 'domain' key
    Returns:
        list of article dicts with added 'reach' key
    """
    reach_dict = fetch_reach_from_s3_file()
    # Domains fetched from the API this run (not already in the S3 file).
    new_reach: dict[str, int] = {}

    for article in articles:
        domain = article.get('domain')
        if domain and domain not in reach_dict:
            # reach = int(fetch_similarweb_reach(domain))
            # article['reach'] = reach
            # reach_dict[domain] = reach          # cache within this run
            # new_reach[domain] = reach           # queue for the S3 write-back
            article['reach'] = 0
        elif domain:
            article['reach'] = int(reach_dict[domain]) if reach_dict[domain] is not None else 0
        else:
            article['reach'] = 0

    # Persist the freshly fetched reaches in the background — the live reach
    # process above is already complete and unaffected by the upload.
    update_reach_file_in_background(new_reach)

    return articles
