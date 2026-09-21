"""Offline tests for brand_hero.py's brand-website-first banner resolution.

No test runner is configured in this repo (see CLAUDE.md); run these the same
way as tests/test_verbatim_preview.py — import the module and call each
test_* function directly.
"""

import asyncio

from consumer_intelligence import brand_hero as bh


async def _no_render(url):
    """Every offline test below drives _scrape_site through its httpx fallback
    (FakeClient), not a real headless browser — disable rendering globally so
    the suite never launches Playwright or touches the network."""
    return None


bh._render_html = _no_render


class FakeResp:
    def __init__(self, text="", content=b"", content_type="text/html", status_code=200):
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode()
        self.headers = {"content-type": content_type}


class FakeClient:
    """Maps a URL prefix to a canned response, in the order given — first match wins."""

    def __init__(self, routes: list[tuple[str, FakeResp]]):
        self.routes = routes
        self.requested: list[str] = []

    async def get(self, url, **kwargs):
        self.requested.append(url)
        exact = next((resp for route_url, resp in self.routes if route_url == url), None)
        if exact:
            return exact
        for prefix, resp in self.routes:  # the homepage route ("https://x/") is a prefix of every sub-path too
            if url.startswith(prefix):
                return resp
        return FakeResp(status_code=404)


def test_extract_social_links_finds_the_brands_own_linked_accounts():
    html = (
        '<footer><a href="https://www.youtube.com/@ArmorAll">YouTube</a>'
        '<a href="https://twitter.com/ArmorAll">X</a>'
        '<a href="https://twitter.com/intent/tweet?text=hi">share this</a></footer>'
    )
    assert bh.extract_social_links(html) == {"youtube": "@ArmorAll", "twitter": "ArmorAll"}


def test_extract_social_links_returns_nothing_for_a_page_with_no_social_links():
    assert bh.extract_social_links("<html><body>Hello</body></html>") == {}


def test_scrape_site_prefers_a_youtube_embed_on_the_homepage_over_everything_else():
    client = FakeClient([
        ("https://brand-a.example/", FakeResp(text=(
            '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>'
            '<meta property="og:image" content="https://brand-a.example/banner.jpg">'
        ))),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-a.example"))
    assert result == {"type": "youtube", "url": "https://www.youtube.com/embed/dQw4w9WgXcQ", "source": "brand_website", "domain": "brand-a.example"}


def test_scrape_site_uses_a_direct_video_file_when_theres_no_youtube_embed():
    client = FakeClient([
        ("https://brand-b.example/", FakeResp(text='<video src="/media/hero.mp4" autoplay muted loop></video>')),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-b.example"))
    assert result == {"type": "video", "url": "https://brand-b.example/media/hero.mp4", "source": "brand_website", "domain": "brand-b.example"}


def test_scrape_site_normalizes_an_og_video_youtube_watch_url_to_an_embed():
    client = FakeClient([
        ("https://brand-c.example/", FakeResp(text='<meta property="og:video" content="https://www.youtube.com/watch?v=abc123XYZ_">')),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-c.example"))
    assert result == {"type": "youtube", "url": "https://www.youtube.com/embed/abc123XYZ_", "source": "brand_website", "domain": "brand-c.example"}


def test_scrape_site_downloads_and_validates_the_og_image():
    real_photo = b"x" * 5000
    client = FakeClient([
        ("https://brand-d.example/", FakeResp(text='<meta property="og:image" content="https://brand-d.example/hero.jpg">')),
        ("https://brand-d.example/hero.jpg", FakeResp(content=real_photo, content_type="image/jpeg")),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-d.example"))
    assert result["type"] == "image" and result["url"] == "https://brand-d.example/hero.jpg"
    assert result["_bytes"] == real_photo and result["_media_type"] == "image/jpeg"


def test_scrape_site_rejects_a_tiny_og_image_as_probably_a_tracking_pixel():
    client = FakeClient([
        ("https://brand-e.example/", FakeResp(text='<meta property="og:image" content="https://brand-e.example/pixel.gif">')),
        ("https://brand-e.example/pixel.gif", FakeResp(content=b"x" * 40, content_type="image/gif")),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-e.example"))
    assert result is None  # falls through to _social_hero, which also finds nothing here


def test_scrape_site_falls_back_to_the_brands_linked_youtube_channels_latest_upload():
    channel_page = '{"channelId":"UCabcdefghijklmno"}'
    feed = "<feed><entry><yt:videoId>zyx987</yt:videoId></entry></feed>"
    client = FakeClient([
        ("https://brand-f.example/", FakeResp(text='<a href="https://www.youtube.com/@BrandF">YouTube</a>')),
        ("https://www.youtube.com/@BrandF", FakeResp(text=channel_page)),
        ("https://www.youtube.com/feeds/videos.xml", FakeResp(text=feed)),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-f.example"))
    assert result == {"type": "youtube", "url": "https://www.youtube.com/embed/zyx987", "source": "brand_youtube", "domain": "brand-f.example"}


def test_scrape_site_falls_back_to_the_brands_twitter_avatar_when_no_youtube_link_exists():
    avatar = b"y" * 6000
    client = FakeClient([
        ("https://brand-g.example/", FakeResp(text='<a href="https://twitter.com/BrandG">X</a>')),
        ("https://unavatar.io/twitter/BrandG", FakeResp(content=avatar, content_type="image/png")),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-g.example"))
    assert result == {"type": "image", "url": "https://unavatar.io/twitter/BrandG", "source": "brand_twitter", "domain": "brand-g.example"}


def test_scrape_site_rejects_unavatars_placeholder_svg_for_an_unknown_handle():
    client = FakeClient([
        ("https://brand-h.example/", FakeResp(text='<a href="https://twitter.com/NobodyHandle">X</a>')),
        ("https://unavatar.io/twitter/NobodyHandle", FakeResp(content=b"<svg></svg>", content_type="image/svg+xml")),
    ])
    result = asyncio.run(bh._scrape_site(client, "brand-h.example"))
    assert result is None


def test_scrape_site_returns_none_when_the_homepage_is_unreachable():
    client = FakeClient([])  # every URL 404s
    assert asyncio.run(bh._scrape_site(client, "unreachable.example")) is None


def test_extract_hero_image_url_finds_a_hero_classed_img_with_no_og_image():
    html = (
        '<div class="site-header">...</div>'
        '<img class="hero-banner lazyload" alt="80th Anniversary" '
        'src="//brand.example/cdn/hero.jpg?v=1" srcset="//brand.example/cdn/hero.jpg?v=1 1x">'
    )
    assert bh._extract_hero_image_url(html, "https://brand.example/") == "https://brand.example/cdn/hero.jpg?v=1"


def test_extract_hero_image_url_unescapes_html_entities_in_the_src():
    html = '<img class="hero-banner" src="/cdn/hero.jpg?v=1&amp;width=3840">'
    assert bh._extract_hero_image_url(html, "https://brand.example/") == "https://brand.example/cdn/hero.jpg?v=1&width=3840"


def test_extract_hero_image_url_returns_none_without_a_hero_classed_img():
    html = '<img class="site-logo" src="/logo.png"><img class="product-thumb" src="/thumb.jpg">'
    assert bh._extract_hero_image_url(html, "https://brand.example/") is None


def test_scrape_site_uses_the_rendered_pages_hero_image_when_theres_no_og_image():
    """A storefront theme that injects its real banner client-side (no
    og:image at all) is exactly what plain HTTP fetching used to miss."""
    real_photo = b"y" * 5000
    rendered_html = '<img class="hero-slide" src="/cdn/hero-banner.jpg">'

    async def fake_render(url):
        return rendered_html

    client = FakeClient([
        ("https://shopify-brand.example/", FakeResp(text="<html><head></head><body>no meta tags here</body></html>")),
        ("https://shopify-brand.example/cdn/hero-banner.jpg", FakeResp(content=real_photo, content_type="image/jpeg")),
    ])
    saved = bh._render_html
    bh._render_html = fake_render
    try:
        result = asyncio.run(bh._scrape_site(client, "shopify-brand.example"))
    finally:
        bh._render_html = saved
    assert result["type"] == "image" and result["url"] == "https://shopify-brand.example/cdn/hero-banner.jpg"
    assert result["_bytes"] == real_photo


def test_resolve_brand_hero_skips_the_vision_check_for_video_and_social_sources():
    async def fake_brand_site_hero(domain):
        return {"type": "youtube", "url": "https://www.youtube.com/embed/abc", "source": "brand_website", "domain": domain}

    saved = bh.brand_site_hero
    bh.brand_site_hero = fake_brand_site_hero
    try:
        result = asyncio.run(bh.resolve_brand_hero("brand-i.example", "Brand I"))
    finally:
        bh.brand_site_hero = saved
    assert result == {"type": "youtube", "url": "https://www.youtube.com/embed/abc", "source": "brand_website", "domain": "brand-i.example"}


def test_resolve_brand_hero_discards_an_image_the_vision_check_says_is_not_the_brand():
    async def fake_brand_site_hero(domain):
        return {"type": "image", "url": "https://brand-j.example/unrelated.jpg", "source": "brand_website", "domain": domain, "_bytes": b"z" * 5000, "_media_type": "image/jpeg"}

    class FakeVisionClient:
        async def image_shows_brand(self, *a, **k):
            return False

    saved_site, saved_vision = bh.brand_site_hero, bh.get_vision_client
    bh.brand_site_hero = fake_brand_site_hero
    bh.get_vision_client = lambda: FakeVisionClient()
    try:
        result = asyncio.run(bh.resolve_brand_hero("brand-j.example", "Brand J"))
    finally:
        bh.brand_site_hero, bh.get_vision_client = saved_site, saved_vision
    assert result is None


def test_resolve_brand_hero_keeps_an_image_the_vision_check_confirms():
    async def fake_brand_site_hero(domain):
        return {"type": "image", "url": "https://brand-k.example/hero.jpg", "source": "brand_website", "domain": domain, "_bytes": b"z" * 5000, "_media_type": "image/jpeg"}

    class FakeVisionClient:
        async def image_shows_brand(self, *a, **k):
            return True

    saved_site, saved_vision = bh.brand_site_hero, bh.get_vision_client
    bh.brand_site_hero = fake_brand_site_hero
    bh.get_vision_client = lambda: FakeVisionClient()
    try:
        result = asyncio.run(bh.resolve_brand_hero("brand-k.example", "Brand K"))
    finally:
        bh.brand_site_hero, bh.get_vision_client = saved_site, saved_vision
    assert result == {"type": "image", "url": "https://brand-k.example/hero.jpg", "source": "brand_website", "domain": "brand-k.example"}
    assert "_bytes" not in result  # internal fields stripped before the caller sees it
