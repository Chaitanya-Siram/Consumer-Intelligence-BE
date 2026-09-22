"""Offline tests for brand_video (official-channel video matching) and the
Brand Intelligence leader-card media chain. Run like tests/test_brand_hero.py."""

import asyncio

from consumer_intelligence import brand_hero, brand_media, brand_video
from consumer_intelligence.storyboard import brand_intel

FEED = """<feed>
<entry><yt:videoId>aaaaaaaaaaa</yt:videoId><title>Tire Shine Spray &amp; Trim Protectant</title></entry>
<entry><yt:videoId>bbbbbbbbbbb</yt:videoId><title>How to wash your car at home</title></entry>
<entry><yt:videoId>ccccccccccc</yt:videoId><title>Ceramic wax review</title></entry>
</feed>"""


def test_parse_feed_reads_ids_and_unescaped_titles_newest_first():
    v = brand_video.parse_feed(FEED)
    assert [x["id"] for x in v] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert v[0]["title"] == "Tire Shine Spray & Trim Protectant"
    assert brand_video.parse_feed("") == [] and brand_video.parse_feed(None) == []


def test_the_video_matching_the_label_is_picked_and_the_brand_name_is_ignored():
    v = brand_video.parse_feed(FEED)
    assert brand_video.pick_related(v, "Car Wash", "Armor All")["id"] == "bbbbbbbbbbb"
    assert brand_video.pick_related(v, "Ceramic Wax", "Armor All")["id"] == "ccccccccccc"


def test_no_related_title_means_no_video_rather_than_an_unrelated_one():
    v = brand_video.parse_feed(FEED)
    assert brand_video.pick_related(v, "Regional Sentiment Shifts", "Armor All") is None
    assert brand_video.pick_related(v, "Armor All", "Armor All") is None  # only the brand's own name
    assert brand_video.pick_related([], "Car Wash", "Armor All") is None


def test_a_tie_keeps_the_newer_video():
    v = [{"id": "new", "title": "Wax tips"}, {"id": "old", "title": "Wax guide"}]
    assert brand_video.pick_related(v, "Wax", "X")["id"] == "new"


def test_leader_video_returns_an_embed_url_for_the_matching_upload():
    async def channel(domain, brand=""):
        return brand_video.parse_feed(FEED)

    saved = brand_video.official_channel_videos
    brand_video.official_channel_videos = channel
    try:
        media = asyncio.run(brand_video.leader_video("armorall.com", "Ceramic Coating Wax", "Armor All"))
    finally:
        brand_video.official_channel_videos = saved
    assert media["type"] == "youtube" and media["url"] == "https://www.youtube.com/embed/ccccccccccc" and media["source"] == "brand_youtube"


def _storyboard():
    return {"tabs": [
        {"label": "Car Wash", "leaders": {"items": [{"name": "Armor All", "media": None}, {"name": "Meguiars", "media": None}]}},
        {"label": "Strategic Intelligence"},
    ]}


def _patch(video=None, hero=None, stock=None):
    async def v(domain, label, brand):
        return video(brand) if video else None

    async def h(domain, brand):
        return hero(brand) if hero else None

    async def s(query):
        return stock(query) if stock else None

    saved = (brand_video.leader_video, brand_hero.resolve_brand_hero, brand_media.stock_photo)
    brand_video.leader_video, brand_hero.resolve_brand_hero, brand_media.stock_photo = v, h, s
    return saved


def _unpatch(saved):
    brand_video.leader_video, brand_hero.resolve_brand_hero, brand_media.stock_photo = saved


def test_leader_media_prefers_the_official_channel_video_then_the_hero_then_a_stock_photo():
    sb = _storyboard()
    saved = _patch(
        video=lambda b: {"type": "youtube", "url": "yt"} if b == "Armor All" else None,
        hero=lambda b: {"type": "image", "url": "hero"} if b == "Meguiars" else None,
        stock=lambda q: {"type": "image", "url": "stock:" + q},
    )
    try:
        asyncio.run(brand_intel.resolve_leader_media(sb, []))
    finally:
        _unpatch(saved)
    items = sb["tabs"][0]["leaders"]["items"]
    assert items[0]["media"]["url"] == "yt" and items[1]["media"]["url"] == "hero"


def test_leader_media_falls_back_to_a_stock_photo_of_brand_and_label():
    sb = _storyboard()
    saved = _patch(stock=lambda q: {"type": "image", "url": "stock:" + q})
    try:
        asyncio.run(brand_intel.resolve_leader_media(sb, []))
    finally:
        _unpatch(saved)
    assert sb["tabs"][0]["leaders"]["items"][0]["media"]["url"] == "stock:Armor All Car Wash"


def test_a_leader_no_source_has_anything_for_keeps_media_none_and_errors_do_not_escape():
    sb = _storyboard()
    saved = _patch()
    try:
        asyncio.run(brand_intel.resolve_leader_media(sb, []))
    finally:
        _unpatch(saved)
    assert all(i["media"] is None for i in sb["tabs"][0]["leaders"]["items"])


def test_home_channel_paths_finds_every_form_of_channel_link_and_skips_player_and_page_urls():
    html = (
        '<a href="https://youtube.com/turtlewax">YT</a>'
        '<a href="https://www.youtube.com/@ChemicalGuys?sub=1">x</a>'
        '<a href="https://www.youtube.com/channel/UCgEcPol_nG9_aFJbB8ameLg">y</a>'
        '<a href="https://www.youtube.com/user/armorall/">z</a>'
        '<a href="https://www.youtube.com/watch">not a channel</a>'
        '<script src="https://www.youtube.com/iframe_api"></script>'
        '<a href="https://www.youtube.com/embed/">not a channel</a>'
    )
    assert brand_video.home_channel_paths(html) == ["turtlewax", "@ChemicalGuys", "channel/UCgEcPol_nG9_aFJbB8ameLg", "user/armorall"]
    assert brand_video.home_channel_paths("") == []


def test_own_channel_id_prefers_the_pages_canonical_link_over_a_stray_channel_id():
    page = '"channelId":"UCstrayOtherChannel1234567" <link rel="canonical" href="https://www.youtube.com/channel/UCownChannelIdabcdefghij">'
    assert brand_video.own_channel_id(page) == "UCownChannelIdabcdefghij"
    assert brand_video.own_channel_id('"externalId":"UCexternalIdabcdefghijkl"') == "UCexternalIdabcdefghijkl"
    assert brand_video.own_channel_id('"channelId":"UCstrayOtherChannel1234567"') is None  # not trusted alone


def test_a_search_hit_is_only_accepted_when_its_page_links_back_to_the_brand():
    pages = {"user/armorall": "<html>Visit armorall.com" + '<link rel="canonical" href="https://www.youtube.com/channel/UCarmorallOfficial12345">',
             "user/fanpage": "<html>a fan of cars" + '<link rel="canonical" href="https://www.youtube.com/channel/UCfanChannelabcdefghijk">'}

    async def get(client, url):
        return pages.get(url.split("youtube.com/")[1])

    saved = brand_hero._get
    brand_hero._get = get
    try:
        official = asyncio.run(brand_video._channel_id(None, "user/armorall", must_link_to="armorall.com"))
        fan = asyncio.run(brand_video._channel_id(None, "user/fanpage", must_link_to="armorall.com"))
        trusted_homepage_link = asyncio.run(brand_video._channel_id(None, "user/fanpage"))
    finally:
        brand_hero._get = saved
    assert official == "UCarmorallOfficial12345"
    assert fan is None
    assert trusted_homepage_link == "UCfanChannelabcdefghijk"  # a channel the brand's own site links to needs no proof


def test_an_empty_channel_result_expires_but_a_found_channel_is_kept():
    import time

    brand_video._CHANNEL_CACHE.clear()
    brand_video._CHANNEL_CACHE["gone.example"] = (time.time() - brand_video._EMPTY_TTL_S - 1, [])
    brand_video._CHANNEL_CACHE["kept.example"] = (time.time() - 10 * brand_video._EMPTY_TTL_S, [{"id": "v", "title": "t"}])
    calls = []

    async def fake_render(url):
        calls.append(url)
        return None

    saved = brand_hero._render_html
    brand_hero._render_html = fake_render
    try:
        asyncio.run(brand_video.official_channel_videos("gone.example", ""))
        kept = asyncio.run(brand_video.official_channel_videos("kept.example", ""))
    finally:
        brand_hero._render_html = saved
        brand_video._CHANNEL_CACHE.clear()
    assert calls == ["https://gone.example/"]  # the stale empty was looked up again; the found channel was not
    assert kept[0]["id"] == "v"


def test_guessed_channel_paths_come_from_the_brand_name():
    assert brand_video.guessed_channel_paths("Armor All") == ["@armorall", "user/armorall", "c/armorall"]
    assert brand_video.guessed_channel_paths("Meguiar's") == ["@meguiars", "user/meguiars", "c/meguiars"]
    assert brand_video.guessed_channel_paths("3M") == []  # too short to guess from


def test_parse_search_hit_reads_the_top_result_and_decodes_escaped_titles():
    page = 'x"videoRenderer":{"videoId":"ykNZQ4Y3K5Y","thumbnail":{},"title":{"runs":[{"text":"HOW TO USE: Protect \u0026 Shine"}]},"other"'
    assert brand_video.parse_search_hit(page) == {"id": "ykNZQ4Y3K5Y", "title": "HOW TO USE: Protect & Shine"}
    assert brand_video.parse_search_hit("<html>no results</html>") is None and brand_video.parse_search_hit(None) is None


def test_a_label_with_no_title_match_falls_back_to_a_search_inside_the_official_channel():
    async def channel(domain, brand=""):
        return brand_video.parse_feed(FEED)

    async def search(channel_id, label, brand):
        assert channel_id == "UCchan" and label == "Foam Cannon"
        return {"id": "ddddddddddd", "title": "Foam cannon setup guide"}

    saved = (brand_video.official_channel_videos, brand_video._channel_search)
    brand_video.official_channel_videos, brand_video._channel_search = channel, search
    brand_video._CHANNEL_ID["x.example"] = "UCchan"
    try:
        media = asyncio.run(brand_video.leader_video("x.example", "Foam Cannon", "Armor All"))
        title_match = asyncio.run(brand_video.leader_video("x.example", "Ceramic Wax", "Armor All"))
    finally:
        brand_video.official_channel_videos, brand_video._channel_search = saved
        brand_video._CHANNEL_ID.pop("x.example", None)
    assert media["url"].endswith("ddddddddddd")
    assert title_match["url"].endswith("ccccccccccc")  # a title match never needs the channel search


def test_words_match_on_stems_so_promo_meets_promotion():
    assert brand_video.pick_related([{"id": "a", "title": "Big promo weekend"}], "Product Promotion", "Armor All")["id"] == "a"


def test_a_channel_search_hit_that_shares_no_word_with_the_label_is_discarded():
    async def channel(domain, brand=""):
        return []

    async def search(channel_id, label, brand):
        return {"id": "nissan00000", "title": "First Look: 2022 Nissan Frontier Pro-4X"}

    saved = (brand_video.official_channel_videos, brand_video._channel_search)
    brand_video.official_channel_videos, brand_video._channel_search = channel, search
    brand_video._CHANNEL_ID["x.example"] = "UCchan"
    try:
        media = asyncio.run(brand_video.leader_video("x.example", "Product Comparison", "Carpro"))
    finally:
        brand_video.official_channel_videos, brand_video._channel_search = saved
        brand_video._CHANNEL_ID.pop("x.example", None)
    assert media is None


def test_compass_is_not_comparison_but_promo_is_promotion_and_compare_is_comparison():
    assert not brand_video._same_word("compass", "comparison")
    assert brand_video._same_word("promo", "promotion") and brand_video._same_word("compare", "comparison")
    assert brand_video.pick_related([{"id": "j", "title": "2023 Jeep Compass Test Drive And Review"}], "Product Comparison", "Carpro") is None
