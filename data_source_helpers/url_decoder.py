import random
import time
import requests, json
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from configs import logger

# A real browser User-Agent — Google returns 400/403 for header-less requests.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_POOL_SIZE = 100

# One shared session = connection pooling + keep-alive (avoids a new TCP/TLS
# handshake per request, which is a big part of the slowness).
session = requests.Session()
session.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
session.mount("https://", _adapter)
session.mount("http://", _adapter)

# (connect, read). Mandatory, not tuning: requests waits forever without it
_REQUEST_TIMEOUT = (5, 15)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 5.0


_TOTAL_DEADLINE_SECONDS = 40.0


def _retry_delay(attempt, retry_after):
    """Seconds to wait before retrying.

    Args:
        attempt: The attempt that just failed (1-based).
        retry_after: The response's Retry-After header value, if any.

    Returns:
        Delay in seconds, capped so one slow retry can't blow the stage deadline.
    """
    if retry_after:
        try:
            return min(float(retry_after), _MAX_BACKOFF_SECONDS)
        except ValueError:
            pass  # HTTP-date form — fall through to the computed backoff.
    delay = min(_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)
    # Jitter, because ~100 decodes are in flight: without it the retries of a
    # throttled burst all land together and get throttled again.
    return delay * (0.5 + random.random())


def request_with_retry(method, url, **kwargs):
    """Issue a request against news.google.com, retrying a throttle/transport failure.

    Also used for the RSS feed fetch in google_news_rss: the feed document comes
    from the same throttled host, and a 503 there loses every article for the
    query rather than just one url.

    Args:
        method: "get" or "post".
        url: Target URL.
        **kwargs: Passed through to requests; `timeout` defaults to _REQUEST_TIMEOUT.

    Returns:
        The successful response.
    """
    kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
    started = time.monotonic()

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        last = attempt == _MAX_ATTEMPTS
        
        resp = session.request(method, url, **kwargs)
        if last or resp.status_code not in _RETRY_STATUSES:
            resp.raise_for_status()
            return resp
        reason, retry_after = f"HTTP {resp.status_code}", resp.headers.get("Retry-After")

        delay = _retry_delay(attempt, retry_after)
        
        elapsed = time.monotonic() - started
        if elapsed + delay + _REQUEST_TIMEOUT[1] > _TOTAL_DEADLINE_SECONDS:
            logger.info(
                f"Google News request: {reason} on {url[:80]}; giving up after "
                f"{elapsed:.0f}s ({attempt}/{_MAX_ATTEMPTS} attempts, per-call budget spent)"
            )
            resp.raise_for_status()
            return resp
        
        logger.info(
            f"Google News request: {reason} on {url[:80]}; "
            f"retrying in {delay:.1f}s (attempt {attempt}/{_MAX_ATTEMPTS})"
        )
        time.sleep(delay)


def decode_google_news_url(source_url):
    if "/articles/" not in source_url:
        raise ValueError(f"Not a decodable Google News article URL: {source_url[:120]}")

    # 1. Get the signature + timestamp from the article page
    r = request_with_retry("get", source_url)
    soup = BeautifulSoup(r.text, "html.parser")
    div = soup.select_one("c-wiz > div")
    if div is None or div.get("data-n-a-sg") is None:
        raise RuntimeError("Could not find signature on page (format may have changed).")
    sig = div.get("data-n-a-sg")
    ts  = div.get("data-n-a-ts")
    art_id = source_url.split("/articles/")[1].split("?")[0]

    # 2. Build the batchexecute payload
    payload = [
        "Fbv4je",
        f'["garturlreq",[["X","X",["X","X"],null,null,1,1,'
        f'"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{art_id}",{ts},"{sig}"]'
    ]
    body = f"f.req={json.dumps([[payload]])}"

    resp = request_with_retry(
        "post",
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=body,
    )

    # 3. Parse the real URL out of the response
    parsed = json.loads(resp.text.split("\n\n")[1])[:-2]
    decoded = json.loads(parsed[0][2])[1]
    return decoded
