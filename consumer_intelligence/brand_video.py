"""A brand's own YouTube video that matches a topic — for the "Brands Leading
This Space" cards, which carry one asset per brand.

"Official channel" is not guessed from a search: it is the channel the brand's
own website links to (the same footer/header link `brand_hero` reads), so a fan
or reseller channel can never stand in for it. Its uploads come from YouTube's
public per-channel RSS feed (free, no API key), and the one whose title best
matches the tab's label wins. A channel with nothing related to the label
returns nothing — an unrelated video is worse than none, and the caller falls
back to the brand's hero image and then a stock photo.

Every lookup is cached per domain for the process's lifetime, so a session with
many tabs costs one homepage read and one feed fetch per brand.

Never raises.
"""

import asyncio
import json
import logging
import re
import time
from html import unescape
from urllib.parse import quote_plus

from . import brand_hero

logger = logging.getLogger(__name__)

_MAX_VIDEOS = 15  # what the RSS feed carries
_MAX_CANDIDATE_CHANNELS = 3   # channels a homepage links to, tried in order
_MAX_SEARCH_CANDIDATES = 6    # search hits verified, most relevant first
_EMPTY_TTL_S = 900            # "no channel found" is retried after this long
_HOME_YT_RX = re.compile(
    r"""href=["']https?://(?:www\.)?youtube\.com/(@[\w.-]+|channel/UC[\w-]+|c/[\w-]+|user/[\w-]+|[A-Za-z0-9_-]{3,40})/?(?:[?#][^"']*)?["']""",
    re.I,
)
_SEARCH_CHANNEL_RX = re.compile(r"youtube\.com/(@[\w.-]+|channel/UC[\w-]+|c/[\w-]+|user/[\w-]+)", re.I)
_CANONICAL_RX = re.compile(r"""<link rel=["']canonical["'] href=["']https://www\.youtube\.com/channel/(UC[\w-]{10,})["']""", re.I)
_EXTERNAL_ID_RX = re.compile(r'"externalId":"(UC[\w-]{10,})"')
_SEARCH_HIT_RX = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"(.*?)"\}', re.S)
# Single-segment youtube.com paths that are pages, not a channel's custom URL.
_RESERVED_PATHS = frozenset(
    "watch embed playlist results feed shorts iframe_api s about t hashtag redirect premium music gaming live account new "
    "signin intl yt howyoutubeworks channel c user".split()
)
_ENTRY_RX = re.compile(r"<entry>(.*?)</entry>", re.S)
_ENTRY_ID_RX = re.compile(r"<yt:videoId>([\w-]{6,})</yt:videoId>")
_ENTRY_TITLE_RX = re.compile(r"<title>(.*?)</title>", re.S)
_TOKEN_RX = re.compile(r"[a-z0-9]+")
# Words that say nothing about a topic and would match every video title.
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it its of on or the this that to with what why your our new best top "
    "vs versus trend trends analysis overview insights index".split()
)

# domain -> the official channel's id, once found (for searching inside it).
_CHANNEL_ID: dict[str, str] = {}
# domain -> (when looked up, [{"id", "title"}] of the official channel's latest
# uploads). A found channel is kept for the process; an empty result expires.
_CHANNEL_CACHE: dict[str, tuple[float, list[dict]]] = {}


_SHARED_PREFIX_CHARS = 6
_MIN_PREFIX_CHARS = 4  # "pro" is not the start of "product"


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RX.findall(str(text or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def _same_word(a: str, b: str) -> bool:
    """Two words that name the same thing: equal, one the start of the other
    ("promo" / "promotion"), or sharing a six-letter start ("compare" /
    "comparison") — but not "compass" / "comparison"."""
    if a == b:
        return True
    if min(len(a), len(b)) >= _MIN_PREFIX_CHARS and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= _SHARED_PREFIX_CHARS and len(b) >= _SHARED_PREFIX_CHARS and a[:_SHARED_PREFIX_CHARS] == b[:_SHARED_PREFIX_CHARS]


def _overlap(wanted: set[str], title: str) -> int:
    """How many of the wanted words the title has."""
    words = _tokens(title)
    return sum(1 for w in wanted if any(_same_word(w, t) for t in words))


def parse_feed(feed: str) -> list[dict]:
    """[{"id", "title"}] from a YouTube channel RSS feed, newest first. Pure."""
    videos = []
    for entry in _ENTRY_RX.findall(feed or ""):
        vid, title = _ENTRY_ID_RX.search(entry), _ENTRY_TITLE_RX.search(entry)
        if vid and title:
            videos.append({"id": vid.group(1), "title": unescape(title.group(1)).strip()})
    return videos[:_MAX_VIDEOS]


def pick_related(videos: list[dict], label: str, brand: str = "") -> dict | None:
    """The video whose title shares the most words with `label`, newest winning
    a tie; None when no title shares any. The brand's own name is ignored — every
    video on its channel matches it. Pure."""
    wanted = {w for w in _tokens(label) if not any(_same_word(w, b) for b in _tokens(brand))}
    if not wanted:
        return None
    best, best_score = None, 0
    for video in videos:  # newest first, so a tie keeps the newer video
        score = _overlap(wanted, video["title"])
        if score > best_score:
            best, best_score = video, score
    return best


def home_channel_paths(html: str) -> list[str]:
    """YouTube channel paths a page links to, in any form the platform has used
    (@handle, channel/UC…, c/name, user/name, or a bare custom URL such as
    youtube.com/turtlewax), first-linked first. Pure."""
    paths: list[str] = []
    for match in _HOME_YT_RX.finditer(html or ""):
        path = match.group(1)
        if path.lower() in _RESERVED_PATHS or path in paths:
            continue
        paths.append(path)
    return paths


def own_channel_id(html: str) -> str | None:
    """The channel a YouTube channel page is *about*. The page's first
    `"channelId"` is not reliable (related channels appear before it); the
    canonical link and `externalId` are the page's own. Pure."""
    for rx in (_CANONICAL_RX, _EXTERNAL_ID_RX):
        match = rx.search(html or "")
        if match:
            return match.group(1)
    return None


async def _channel_id(client, path: str, *, must_link_to: str | None = None) -> str | None:
    """Channel id for a channel path. With `must_link_to`, the channel's own page
    has to mention that domain — the proof a search hit is the brand's channel
    and not a fan's or a reseller's."""
    if path.startswith("channel/UC") and not must_link_to:
        return path.split("/", 1)[1]
    page = await brand_hero._get(client, f"https://www.youtube.com/{path}")  # noqa: SLF001
    if not page or (must_link_to and must_link_to.lower() not in page.lower()):
        return None
    return own_channel_id(page)


def guessed_channel_paths(brand: str) -> list[str]:
    """The URLs a brand's channel most often lives at, from its name: "Armor All"
    -> @armorall, user/armorall, c/armorall. Unverified — the caller checks each
    page links back to the brand's site. Pure."""
    slug = re.sub(r"[^a-z0-9]", "", str(brand or "").lower())
    return [f"@{slug}", f"user/{slug}", f"c/{slug}"] if len(slug) >= 3 else []


async def _search_channel_paths(brand: str) -> list[str]:
    """Candidate channel paths for a brand from a web search (unverified)."""
    paths: list[str] = []
    for query in (f'"{brand}" official YouTube channel', f"{brand} youtube channel", f"site:youtube.com {brand}"):
        try:
            from ddgs import DDGS

            results = await asyncio.to_thread(lambda q=query: DDGS().text(q, max_results=8))
        except Exception as exc:  # the search is flaky: another phrasing may still work
            logger.debug("channel search failed for %r: %s", query, exc)
            continue
        for result in results or []:
            match = _SEARCH_CHANNEL_RX.search(str(result.get("href") or ""))
            if match and match.group(1) not in paths:
                paths.append(match.group(1))
        if len(paths) >= _MAX_SEARCH_CANDIDATES:
            break
    return paths[:_MAX_SEARCH_CANDIDATES]


async def official_channel_videos(domain: str | None, brand: str = "") -> list[dict]:
    """The brand's official channel's latest uploads. The channel is the one its
    own homepage links to; when the site links to none (or can't be read), a web
    search proposes candidates and one is accepted only if its own page links
    back to the brand's domain. [] when neither finds a channel."""
    if not domain:
        return []
    cached = _CHANNEL_CACHE.get(domain)
    if cached and (cached[1] or time.time() - cached[0] < _EMPTY_TTL_S):
        return cached[1]
    videos: list[dict] = []
    try:
        import httpx

        home = f"https://{domain}/"
        async with httpx.AsyncClient() as client:
            html = await brand_hero._render_html(home) or await brand_hero._get(client, home)  # noqa: SLF001
            channel_id = None
            for path in home_channel_paths(html or "")[:_MAX_CANDIDATE_CHANNELS]:
                channel_id = await _channel_id(client, path)
                if channel_id:
                    break
            if not channel_id and brand:
                # The usual URL for a brand's channel first (deterministic), a web
                # search only as the backup — each proven by a link back to the site.
                for path in [*guessed_channel_paths(brand), *(await _search_channel_paths(brand))]:
                    channel_id = await _channel_id(client, path, must_link_to=domain)
                    if channel_id:
                        break
            if channel_id:
                _CHANNEL_ID[domain] = channel_id
                feed = await brand_hero._get(client, f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")  # noqa: SLF001
                videos = parse_feed(feed or "")
    except Exception as exc:
        logger.debug("official channel lookup failed for %s: %s", domain, exc)
    _CHANNEL_CACHE[domain] = (time.time(), videos)
    return videos


def parse_search_hit(page: str) -> dict | None:
    """The top result of a YouTube channel-search page as {"id", "title"}. Pure."""
    match = _SEARCH_HIT_RX.search(page or "")
    if not match:
        return None
    try:
        title = json.loads(f'"{match.group(2)}"')  # the page JSON-escapes & and friends
    except ValueError:
        title = match.group(2)
    return {"id": match.group(1), "title": unescape(title).strip()}


async def _channel_search(channel_id: str, label: str, brand: str) -> dict | None:
    """The official channel's own top result for the label — YouTube's search
    over the channel's *whole* catalogue, not just its latest uploads. None when
    the channel has nothing on the topic."""
    words = " ".join(w for w in _TOKEN_RX.findall(label.lower()) if w in _tokens(label) and not any(_same_word(w, b) for b in _tokens(brand)))
    if not words:
        return None
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            page = await brand_hero._get(client, f"https://www.youtube.com/channel/{channel_id}/search?query={quote_plus(words)}")  # noqa: SLF001
    except Exception as exc:
        logger.debug("channel search failed for %s: %s", channel_id, exc)
        return None
    return parse_search_hit(page or "")


async def leader_video(domain: str | None, label: str, brand: str) -> dict | None:
    """`{"type": "youtube", "url": <embed>, ...}` for the official-channel video
    that best matches `label`, or None: first among the channel's latest uploads,
    then anywhere in its catalogue."""
    video = pick_related(await official_channel_videos(domain, brand), label, brand)
    if not video and _CHANNEL_ID.get(domain or ""):
        hit = await _channel_search(_CHANNEL_ID[domain], label, brand)
        # YouTube's search also matches descriptions and tags: keep a hit only if
        # its own title shares a word with the label.
        video = pick_related([hit], label, brand) if hit else None
    if not video:
        return None
    return {"type": "youtube", "url": f"https://www.youtube.com/embed/{video['id']}", "title": video["title"], "source": "brand_youtube", "domain": domain}
