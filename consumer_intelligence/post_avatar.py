"""The poster's own avatar, read from the page their post lives on.

Forum threads (XenForo, vBulletin, phpBB, Discourse, ...) and retailer review
pages show each poster's picture beside their username, but there is no API to
ask for it. `find_avatar` locates it in the page's HTML; `fetch_avatar_url`
gets that HTML with scrapling, whose stealth fetcher gets past the anti-bot
challenges most of these sites sit behind.

A page holds many posters, so a picture is only trusted when it can be tied to
this author: an avatar whose alt/title is the username, or the single avatar
in the smallest block that contains the username. If that block holds several
avatars the match is ambiguous and nothing is returned — a missing picture
(the FE draws initials) is better than another member's face.
"""

import logging
import re
from urllib.parse import urljoin, urldefrag

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 30
_STEALTH_TIMEOUT_MS = 45000
_MAX_NAME_MATCHES = 8
_MAX_CLIMB = 7
_MIN_HTML_BYTES = 6000  # a challenge or interstitial page is a few KB

_AVATAR_HINT_RX = re.compile(r"avatar|userpic|user-pic|profile[-_ ]?(?:pic|image|photo)|gravatar|member[-_ ]?(?:pic|photo)", re.I)
_PLACEHOLDER_RX = re.compile(
    r"no[-_]?avatar|noavatar|default|placeholder|blank|anonymous|guest|spacer|smilie|emoji|/logo|sprite|loading|pixel\.gif|d=(?:mm|mp|blank|identicon)|\.svg|rank|badge|medal|trophy|reaction",
    re.I,
)
_IMG_URL_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url", "src")


def _text(value) -> str:
    return " ".join(value) if isinstance(value, list) else str(value or "")


def _img_url(img, base_url: str) -> str | None:
    """The picture's real URL: a lazy-loaded image keeps it in a data-* attribute
    and puts a placeholder data: URI in `src`."""
    for attr in _IMG_URL_ATTRS:
        value = str(img.get(attr) or "").strip()
        if value and not value.startswith("data:"):
            return urljoin(base_url, value)
    return None


def _is_avatar(img) -> bool:
    blob = " ".join(_text(img.get(a)) for a in ("class", "id", "src", "data-src", "alt", "title"))
    return bool(_AVATAR_HINT_RX.search(blob))


def _usable(url: str | None) -> bool:
    return bool(url) and not _PLACEHOLDER_RX.search(url)


def find_avatar(html: str, author: str, base_url: str = "") -> str | None:
    """Absolute URL of `author`'s avatar in `html`, or None when it can't be
    tied to that author with confidence."""
    from bs4 import BeautifulSoup

    name = str(author or "").strip().lstrip("@").lower()
    if not name or not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # An avatar that names its owner ("Davesrb's avatar", alt="Davesrb").
    for img in soup.find_all("img"):
        label = f"{_text(img.get('alt'))} {_text(img.get('title'))}".lower()
        if name in label and (_is_avatar(img) or label.strip() == name):
            url = _img_url(img, base_url)
            if _usable(url):
                return url

    # The one avatar in the tightest block around the username.
    for node in soup.find_all(string=lambda s: bool(s) and s.strip().lower() == name)[:_MAX_NAME_MATCHES]:
        block = node.parent
        for _ in range(_MAX_CLIMB):
            block = block.parent if block is not None else None
            if block is None:
                break
            avatars = [i for i in block.find_all("img") if _is_avatar(i) and _img_url(i, base_url)]
            if len(avatars) > 1:
                break  # the block spans several posters: ambiguous
            if len(avatars) == 1:
                url = _img_url(avatars[0], base_url)
                if _usable(url):
                    return url
                break
    return None


def _page_html(url: str) -> str:
    """Blocking. The page's HTML, through the plain fetcher first and the
    Cloudflare-solving stealth one when that only returns a challenge."""
    from scrapling.fetchers import Fetcher, StealthyFetcher

    try:
        page = Fetcher.get(url, stealthy_headers=True, timeout=_FETCH_TIMEOUT_S)
        html = page.html_content if page.status == 200 else ""
        if len(html) >= _MIN_HTML_BYTES:
            return html
    except Exception as exc:
        logger.debug("post page plain fetch failed for %s: %s", url, exc)
    page = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True, network_idle=False, timeout=_STEALTH_TIMEOUT_MS)
    return page.html_content if page.status == 200 else ""


def fetch_avatar_url(post_url: str, author: str) -> str | None:
    """Blocking. Fetch the post's page and pull the author's avatar from it.
    Never raises."""
    page_url = urldefrag(str(post_url or ""))[0]
    if not page_url.startswith("http"):
        return None
    try:
        return find_avatar(_page_html(page_url), author, page_url)
    except Exception as exc:
        logger.info("post avatar lookup failed for %s (%s): %s", author, page_url, exc)
        return None
