import requests, json
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# A real browser User-Agent — Google returns 400/403 for header-less requests.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# We decode many Google News URLs concurrently (keyword fan-out × per-keyword
# worker pools), all hitting news.google.com. urllib3's default pool_maxsize=10
# is too small for that, so it discards/recreates connections ("Connection pool
# is full" warning). Size the pool to our concurrency so connections get reused.
_POOL_SIZE = 100

# One shared session = connection pooling + keep-alive (avoids a new TCP/TLS
# handshake per request, which is a big part of the slowness).
session = requests.Session()
session.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
session.mount("https://", _adapter)
session.mount("http://", _adapter)


def decode_google_news_url(source_url):
    # 1. Get the signature + timestamp from the article page
    r = session.get(source_url)
    r.raise_for_status()
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

    resp = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=body,
    )
    resp.raise_for_status()

    # 3. Parse the real URL out of the response
    parsed = json.loads(resp.text.split("\n\n")[1])[:-2]
    decoded = json.loads(parsed[0][2])[1]
    return decoded
