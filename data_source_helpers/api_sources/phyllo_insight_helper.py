from datetime import datetime, timedelta, timezone
import time
import requests
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError,
)
from data_source_helpers.scrapper_utils import (
    extract_mentions_hashtags,
    find_matched_keywords,
    matches_boolean_query,
    split_boolean_query,
)
from configs import logger, envs

_QUERY_TIMEOUT = 1200
_JOB_STATUS_POLL_INTERVAL = 5
_JOB_STATUS_TIMEOUT = 600

class PhylloInsightAPIHelper:
    def __init__(self):
        self.API_BASE_URL = "https://api.staging.insightiq.ai/v1/social/creators/contents/search"
        self.API_JOB_STATUS_URL = "https://api.staging.insightiq.ai/v1/social/creators/contents/search"
        self.API_DATA_URL = "https://api.staging.insightiq.ai/v1/social/creators/contents/search/{JOB_ID}/fetch"
        self.TWITTER_ID = "7645460a-96e0-4192-a3ce-a1fc30641f72"
        self.INSTAGRAM_ID = "9bb8913b-ddd9-430b-a66a-d74d846e6c66"
        self.REDDIT_ID = "dfe5c762-10b2-44fd-b3f2-2c6387690da8"
        self.YOUTUBE_ID = "14d9ddf5-51c6-415e-bde6-f8ed36ad7054"

    def clean_queries(self, queries: list[dict[str, str]] | list[str]) -> list[dict[str, str]]:
        """
        Clean and standardize the input queries.

        Args:
            queries (list[dict[str, str]] | list[str]): The input queries to clean.
        """
        query_meta: dict[str, dict] = {}
        for entry in queries:
            if isinstance(entry, dict):
                query = str(entry.get("query") or "").strip()
                group = entry.get("group")
            else:
                query = str(entry or "").strip()
                group = None
            if not query or query in query_meta:
                continue
            query_meta[query] = {"query": query, "group": group}
        return query_meta

    def _date_params(self, work_platform_id: str, recency_hours: int) -> dict:
        """Build the platform's date filter for a create job request.

        Args:
            work_platform_id: Phyllo work platform id.
            recency_hours: How far back the results should reach.

        Returns:
            Date filter params for the create job payload.
        """
        # Only Twitter takes an explicit range; the others take a coarse bucket.
        if work_platform_id == self.TWITTER_ID:
            now = datetime.now(timezone.utc)
            return {
                "from_date": (now - timedelta(hours=recency_hours)).strftime("%Y-%m-%d"),
                "to_date": now.strftime("%Y-%m-%d"),
            }
        return {"upload_date": "this_week"}

    def create_job(
        self,
        username: str,
        password: str,
        params: dict,
        platform: str,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
    ):
        """
        Create a job to fetch data from the Phyllo Insight API.

        Args:
            username (str): Phyllo client id used as the Basic auth username.
            password (str): Phyllo client secret used as the Basic auth password.
            params (dict): Parameters for the API request.
            recency_hours (int): How far back the results should reach.

        Returns:
            Job id string, or None if the request failed.
        """
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {
            **params,
            **self._date_params(params.get("work_platform_id"), recency_hours),
        }
        try:
            response = requests.post(
                self.API_BASE_URL,
                headers=headers,
                json=payload,
                auth=(username, password),
                timeout=60,
            )
            response.raise_for_status()
            return response.json().get("id")
        except Exception as e:
            logger.error(f"Phyllo Error: create {platform} job failed: {e}")
            return None

    def get_job_status(self, username: str, password: str, job_id: str, platform: str,):
        """
        Poll a job until it succeeds, checking every 5 seconds.

        Args:
            username (str): Phyllo client id used as the Basic auth username.
            password (str): Phyllo client secret used as the Basic auth password.
            job_id (str): The ID of the job to check.

        Returns:
            Job id string once the status is SUCCESS, or None if it failed.
        """
        headers = {"Accept": "application/json"}
        url = f"{self.API_JOB_STATUS_URL}/{job_id}"
        deadline = time.monotonic() + _JOB_STATUS_TIMEOUT
        while time.monotonic() < deadline:
            try:
                response = requests.get(
                    url, headers=headers, auth=(username, password), timeout=60
                )
                response.raise_for_status()
                status = response.json().get("status")
            except Exception as e:
                logger.error(f"Phyllo Error: {platform} job status failed for {job_id}: {e}")
                return None

            if status == "SUCCESS":
                return job_id
            if status in ("FAILURE", "FAILED", "CANCELLED"):
                logger.error(f"Phyllo Error: {platform} job {job_id} ended with status {status}")
                return None
            time.sleep(_JOB_STATUS_POLL_INTERVAL)

        logger.error(f"Phyllo Error: {platform} job {job_id} did not finish within {_JOB_STATUS_TIMEOUT}s")
        return None

    def get_data(
        self,
        username: str,
        password: str,
        job_id: str,
        platform: str,
        total_records: int = 100,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
    ):
        """
        Retrieve the data for a completed job, paginating until total_records.

        Args:
            username (str): Phyllo client id used as the Basic auth username.
            password (str): Phyllo client secret used as the Basic auth password.
            job_id (str): The ID of the completed job to retrieve data for.
            total_records (int): Max number of records to fetch.
            recency_hours (int): How far back the results should reach.

        Returns:
            List of record dicts.
        """
        headers = {"Accept": "application/json"}
        url = self.API_DATA_URL.format(JOB_ID=job_id)
        records = []
        offset = 0
        limit = 100
        now = datetime.now(timezone.utc)
        end_date = now.strftime("%Y-%m-%d")
        start_date = (now - timedelta(hours=recency_hours)).strftime("%Y-%m-%d")
        while offset < total_records:
            params = {
                "offset": offset,
                "limit": min(limit, total_records - offset),
                "from_date": start_date,
                "to_date": end_date,
            }
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    auth=(username, password),
                    timeout=60,
                )
                response.raise_for_status()
                batch = response.json().get("data") or []
            except Exception as e:
                logger.error(f"Phyllo Error: fetch {platform} data failed for {job_id}: {e}")
                break

            records.extend(batch)
            if len(batch) < params["limit"]:
                break
            offset += params["limit"]
        return records

    def run_search(
        self,
        username: str,
        password: str,
        params: dict,
        platform: str,
        total_records: int = 100,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
    ):
        """
        Create a search job, wait for it to finish and fetch its records.

        Args:
            username (str): Phyllo client id used as the Basic auth username.
            password (str): Phyllo client secret used as the Basic auth password.
            params (dict): Parameters for the create job request.
            total_records (int): Max number of records to fetch.
            recency_hours (int): How far back the results should reach.

        Returns:
            List of record dicts.
        """
        job_id = self.create_job(username, password, params, platform, recency_hours)
        if not job_id:
            return []
        if not self.get_job_status(username, password, job_id, platform):
            return []
        return self.get_data(username, password, job_id, platform, total_records, recency_hours)

    def _map_article(self, item: dict, domain: str) -> dict:
        """Shape one Phyllo record into the pipeline's article dict.

        Args:
            item: Raw Phyllo record.
            domain: Platform domain to stamp on the article.

        Returns:
            The mapped article dict.
        """
        engagement = item.get("engagement") or {}
        profile = item.get("profile") or {}
        return {
            "title": item.get("title", ""),
            "content": item.get("description", ""),
            "url": item.get("url", ""),
            "domain": domain,
            "date": item.get("published_at") or "",
            "group": item.get("group", ""),
            "query": item.get("query"),
            "keyword_matched": item.get("keyword_matched"),
            "author": profile.get("platform_username"),
            "media_type": "Social",
            # engagement
            "like_count": engagement.get("like_count"),
            "view_count": engagement.get("view_count"),
            "share_count": engagement.get("share_count"),
            "comment_count": engagement.get("comment_count"),
            "mentions": item.get("mentions"),
            "hashtags": item.get("hashtags"),
            "media_urls": item.get("media_urls"),
        }

    def _tag_payloads(self, query: str, tag_params: tuple[str, ...]) -> list[dict]:
        """Build the mention/hashtag search payloads for one query.

        The API takes only one of keyword/mention/hashtag per call, so each tag
        becomes its own search.

        Args:
            query: Boolean query string.
            tag_params: Tag params the platform supports, e.g. ("hashtag",).

        Returns:
            List of partial payload dicts.
        """
        if not tag_params:
            return []
        tags = extract_mentions_hashtags(query)
        payloads = []
        if "mention" in tag_params:
            payloads += [{"mention": m} for m in tags["mentions"]]
        if "hashtag" in tag_params:
            payloads += [{"hashtag": h} for h in tags["hashtags"]]
        return payloads

    def _fetch_boolean_platform(
        self,
        username: str,
        password: str,
        queries: list[dict[str, str]] | list[str],
        work_platform_id: str,
        domain: str,
        platform: str,
        on_progress=None,
        sort_by: str | None = None,
        tag_params: tuple[str, ...] = (),
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
    ):
        """Fetch one query per search for a platform whose search accepts booleans.

        Args:
            username: Phyllo client id used as the Basic auth username.
            password: Phyllo client secret used as the Basic auth password.
            queries: Boolean query strings, or dicts with query/group.
            work_platform_id: Phyllo work platform id.
            domain: Platform domain stamped on each article.
            platform: Platform name, for logging.
            on_progress: Called with the running article count.
            sort_by: Result ordering; only some platforms support it.
            tag_params: Tag params the platform supports, e.g. ("hashtag",).
            recency_hours: How far back the results should reach.

        Returns:
            List of mapped article dicts.
        """
        total_records = 100
        params = {"work_platform_id": work_platform_id, "items_limit": total_records}
        if sort_by:
            params["sort_by"] = sort_by

        articles = []
        seen_urls = set()
        try:
            query_meta = self.clean_queries(queries)
            searches = [
                (payload, q)
                for q in query_meta
                for payload in [{"keyword": q}] + self._tag_payloads(q, tag_params)
            ]
            if searches:
                max_workers = min(10, len(searches))
                pool = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    futures = {
                        pool.submit(
                            self.run_search,
                            username,
                            password,
                            {**params, **payload},
                            platform,
                            total_records,
                            recency_hours,
                        ): q
                        for payload, q in searches
                    }
                    done = 0
                    try:
                        for fut in as_completed(futures, timeout=_QUERY_TIMEOUT):
                            done += 1
                            query = futures[fut]
                            meta = query_meta[query]
                            try:
                                fetched = fut.result()
                            except Exception as e:
                                logger.exception(f"Phyllo {platform}: fetch failed for {query}: {e}")
                                continue
                            for art in fetched:
                                if not isinstance(art, dict):
                                    continue
                                url = art.get("url")
                                if url and url in seen_urls:
                                    continue
                                if url:
                                    seen_urls.add(url)
                                art.setdefault("query", meta["query"])
                                if meta["group"] is not None:
                                    art.setdefault("group", meta["group"])
                                # Which of the query's terms actually appear in the post.
                                art["keyword_matched"] = find_matched_keywords(
                                    meta["query"], art.get("title"), art.get("description")
                                )
                                articles.append(art)
                            logger.info(f"Phyllo {platform}: articles fetched for {query}")
                            if on_progress is not None:
                                try:
                                    on_progress(len(articles))
                                except Exception:
                                    logger.exception("Phyllo: on_progress callback failed")
                    except FuturesTimeoutError:
                        logger.warning(
                            f"Phyllo {platform}: query fan-out hit its {_QUERY_TIMEOUT}s "
                            f"deadline; continuing with {done} of {len(searches)} search(es)"
                        )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.exception(f"Phyllo {platform} fetch failed: {e}")

        return [self._map_article(item, domain) for item in articles]

    def fetch_phyllo_twitter_data(
            self,
            username: str,
            password: str,
            queries: list[dict[str, str]] | list[str],
            on_progress=None,
            recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
            skip_url=None,
        ):
        """Fetch Twitter data from the Phyllo Insight API.

        Args:
            username: Phyllo client id used as the Basic auth username.
            password: Phyllo client secret used as the Basic auth password.
            queries: Boolean query strings, or dicts with query/group.
            on_progress: Called with the running article count.
            recency_hours: How far back the results should reach.
            skip_url: Unused; kept to match the other fetchers.

        Returns:
            List of mapped article dicts.
        """
        return self._fetch_boolean_platform(
            username,
            password,
            queries,
            self.TWITTER_ID,
            "x.com",
            "twitter",
            on_progress=on_progress,
            tag_params=("mention", "hashtag"),
            recency_hours=recency_hours,
        )

    def fetch_phyllo_reddit_data(
            self,
            username: str,
            password: str,
            queries: list[dict[str, str]] | list[str],
            on_progress=None,
            recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
            skip_url=None,
        ):
        """Fetch Reddit data from the Phyllo Insight API.

        Args:
            username: Phyllo client id used as the Basic auth username.
            password: Phyllo client secret used as the Basic auth password.
            queries: Boolean query strings, or dicts with query/group.
            on_progress: Called with the running article count.
            recency_hours: How far back the results should reach.
            skip_url: Unused; kept to match the other fetchers.

        Returns:
            List of mapped article dicts.
        """
        return self._fetch_boolean_platform(
            username,
            password,
            queries,
            self.REDDIT_ID,
            "reddit.com",
            "reddit",
            on_progress=on_progress,
            # Only Reddit's search accepts sort_by.
            sort_by="latest",
            recency_hours=recency_hours,
        )

    def fetch_phyllo_youtube_data(
            self,
            username: str,
            password: str,
            queries: list[dict[str, str]] | list[str],
            on_progress=None,
            recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
            skip_url=None,
        ):
        """Fetch YouTube data from the Phyllo Insight API.

        Args:
            username: Phyllo client id used as the Basic auth username.
            password: Phyllo client secret used as the Basic auth password.
            queries: Boolean query strings, or dicts with query/group.
            on_progress: Called with the running article count.
            recency_hours: How far back the results should reach.
            skip_url: Unused; kept to match the other fetchers.

        Returns:
            List of mapped article dicts.
        """
        return self._fetch_boolean_platform(
            username,
            password,
            queries,
            self.YOUTUBE_ID,
            "youtube.com",
            "youtube",
            on_progress=on_progress,
            # YouTube search takes a hashtag but not a mention.
            tag_params=("hashtag",),
            recency_hours=recency_hours,
        )

    def fetch_phyllo_instagram_data(
        self,
        username: str,
        password: str,
        queries: list[dict[str, str]] | list[str],
        on_progress=None,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
        skip_url=None,
    ):
        """Fetch Instagram data from the Phyllo Insight API.

        Instagram search takes a single keyword, not a boolean expression, so each
        query is split: every OR term is searched separately and the AND/NOT terms
        are applied to the results here.

        Args:
            username: Phyllo client id used as the Basic auth username.
            password: Phyllo client secret used as the Basic auth password.
            queries: Boolean query strings, or dicts with query/group.
            on_progress: Called with the running article count.
            recency_hours: How far back the results should reach.
            skip_url: Unused; kept to match the other fetchers.

        Returns:
            List of mapped article dicts.
        """
        total_records = 100
        params = {
            "work_platform_id": self.INSTAGRAM_ID,
            "items_limit": total_records,
        }

        articles = []
        seen_urls = set()
        try:
            query_meta = self.clean_queries(queries)
            # One search per OR term, remembering which query it came from.
            searches = []
            for query, meta in query_meta.items():
                groups = split_boolean_query(query)
                terms = groups["or"] or groups["and"]
                for term in terms:
                    searches.append(({"keyword": term}, term, query, groups))
                for payload in self._tag_payloads(query, ("mention", "hashtag")):
                    term = next(iter(payload.values()))
                    searches.append((payload, term, query, groups))

            if searches:
                max_workers = min(10, len(searches))
                pool = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    futures = {
                        pool.submit(
                            self.run_search,
                            username,
                            password,
                            {**params, **payload},
                            total_records,
                            recency_hours,
                        ): (term, query, groups)
                        for payload, term, query, groups in searches
                    }
                    done = 0
                    try:
                        for fut in as_completed(futures, timeout=_QUERY_TIMEOUT):
                            done += 1
                            term, query, groups = futures[fut]
                            meta = query_meta[query]
                            try:
                                fetched = fut.result()
                            except Exception as e:
                                logger.exception(f"Phyllo: fetch failed for {term}: {e}")
                                continue
                            for art in fetched:
                                if not isinstance(art, dict):
                                    continue
                                url = art.get("url")
                                if url and url in seen_urls:
                                    continue
                                # Instagram ignored the AND/NOT parts, so enforce them here.
                                if not matches_boolean_query(
                                    groups, art.get("title"), art.get("description")
                                ):
                                    continue
                                if url:
                                    seen_urls.add(url)
                                art.setdefault("query", meta["query"])
                                if meta["group"] is not None:
                                    art.setdefault("group", meta["group"])
                                art["keyword_matched"] = find_matched_keywords(
                                    meta["query"], art.get("title"), art.get("description")
                                )
                                articles.append(art)
                            logger.info(f"Phyllo: articles fetched for {term}")
                            if on_progress is not None:
                                try:
                                    on_progress(len(articles))
                                except Exception:
                                    logger.exception("Phyllo: on_progress callback failed")
                    except FuturesTimeoutError:
                        logger.warning(
                            f"Phyllo: query fan-out hit its {_QUERY_TIMEOUT}s deadline; "
                            f"continuing with {done} of {len(searches)} search(es)"
                        )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.exception(f"Phyllo fetch_phyllo_instagram_data failed: {e}")

        return [self._map_article(item, "instagram.com") for item in articles]


phyllo_insight_api_helper = PhylloInsightAPIHelper()