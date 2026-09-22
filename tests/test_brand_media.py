"""Offline tests for brand_media.resolve_slot_images (the per-dimension /
per-tab sub-banner photo fill) and stock_photo's DuckDuckGo-then-Pexels
fallback chain. No test runner is configured in this repo (see CLAUDE.md);
run these the same way as tests/test_brand_hero.py.
"""

import asyncio
import sys
import types

from consumer_intelligence import brand_media


def _patch_stock_photo(fake):
    """resolve_slot_images calls stock_photo, not pexels_photo directly, so
    tests that only care about the slot-filling logic mock at that seam."""
    saved = brand_media.stock_photo
    brand_media.stock_photo = fake
    return saved


def test_resolve_slot_images_fills_every_none_image_slot_found_anywhere():
    calls = []

    async def fake_stock_photo(query):
        calls.append(query)
        return {"url": f"https://images.example/{query.replace(' ', '-')}.jpg"}

    storyboard = {
        "hero": {"media": None},  # a different placeholder shape; must be left alone
        "dimensions": [
            {"name": "Awareness", "image": None},
            {"name": "Trust", "image": None},
        ],
        "tabs": [
            {"label": "Overview", "banner": {"title": "Product Recall", "image": None}},
        ],
    }
    saved = _patch_stock_photo(fake_stock_photo)
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care products"))
    finally:
        brand_media.stock_photo = saved

    assert storyboard["dimensions"][0]["image"] == "https://images.example/Armor-All-Car-care-products-Awareness.jpg"
    assert storyboard["dimensions"][1]["image"] == "https://images.example/Armor-All-Car-care-products-Trust.jpg"
    assert storyboard["tabs"][0]["banner"]["image"] is not None
    assert storyboard["hero"]["media"] is None  # untouched: no "image" key at that dict
    assert len(calls) == 3


def test_resolve_slot_images_leaves_a_slot_at_none_when_nothing_is_found():
    async def fake_stock_photo(query):
        return None

    storyboard = {"dimensions": [{"name": "Advocacy", "image": None}]}
    saved = _patch_stock_photo(fake_stock_photo)
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care"))
    finally:
        brand_media.stock_photo = saved
    assert storyboard["dimensions"][0]["image"] is None


def test_resolve_slot_images_never_calls_stock_photo_when_no_slots_need_filling():
    calls = []

    async def fake_stock_photo(query):
        calls.append(query)
        return {"url": "unused"}

    storyboard = {
        "dimensions": [{"name": "Trust", "image": "https://already-resolved.example/x.jpg"}],
        "meta": {"brand": "Armor All"},
    }
    saved = _patch_stock_photo(fake_stock_photo)
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care"))
    finally:
        brand_media.stock_photo = saved
    assert calls == []
    assert storyboard["dimensions"][0]["image"] == "https://already-resolved.example/x.jpg"


def test_slot_label_prefers_name_over_title_over_label_over_eyebrow():
    assert brand_media._slot_label({"name": "Awareness", "title": "x", "image": None}) == "Awareness"
    assert brand_media._slot_label({"title": "Product Recall", "eyebrow": "01 · Overview", "image": None}) == "Product Recall"
    assert brand_media._slot_label({"eyebrow": "BRAND OVERVIEW", "image": None}) == "BRAND OVERVIEW"
    assert brand_media._slot_label({"image": None}) == ""


def test_resolve_slot_images_builds_the_query_from_brand_category_and_label():
    seen = {}

    async def fake_stock_photo(query):
        seen["query"] = query
        return None

    storyboard = {"dimensions": [{"name": "Awareness", "image": None}]}
    saved = _patch_stock_photo(fake_stock_photo)
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care products"))
    finally:
        brand_media.stock_photo = saved
    assert seen["query"] == "Armor All Car care products Awareness"


def test_stock_photo_tries_duckduckgo_before_pexels():
    calls = []

    async def fake_ddg(query):
        calls.append("ddg")
        return {"type": "image", "url": "https://ddg.example/hit.jpg", "source": "duckduckgo"}

    async def fake_pexels(query):
        calls.append("pexels")
        return {"type": "image", "url": "https://pexels.example/hit.jpg", "source": "pexels"}

    saved_ddg, saved_pexels = brand_media.duckduckgo_image, brand_media.pexels_photo
    brand_media.duckduckgo_image, brand_media.pexels_photo = fake_ddg, fake_pexels
    brand_media._STOCK_CACHE.pop("Armor All wipes", None)
    try:
        result = asyncio.run(brand_media.stock_photo("Armor All wipes"))
    finally:
        brand_media.duckduckgo_image, brand_media.pexels_photo = saved_ddg, saved_pexels
        brand_media._STOCK_CACHE.pop("Armor All wipes", None)

    assert result["url"] == "https://ddg.example/hit.jpg"
    assert calls == ["ddg"]  # pexels never called once duckduckgo already found something


def test_stock_photo_falls_back_to_pexels_when_duckduckgo_finds_nothing():
    async def fake_ddg(query):
        return None

    async def fake_pexels(query):
        return {"type": "image", "url": "https://pexels.example/hit.jpg", "source": "pexels"}

    saved_ddg, saved_pexels = brand_media.duckduckgo_image, brand_media.pexels_photo
    brand_media.duckduckgo_image, brand_media.pexels_photo = fake_ddg, fake_pexels
    brand_media._STOCK_CACHE.pop("Armor All shine", None)
    try:
        result = asyncio.run(brand_media.stock_photo("Armor All shine"))
    finally:
        brand_media.duckduckgo_image, brand_media.pexels_photo = saved_ddg, saved_pexels
        brand_media._STOCK_CACHE.pop("Armor All shine", None)

    assert result["url"] == "https://pexels.example/hit.jpg"


def test_stock_photo_caches_so_a_repeated_query_never_rehits_either_provider():
    calls = []

    async def fake_ddg(query):
        calls.append(query)
        return {"type": "image", "url": "https://ddg.example/hit.jpg", "source": "duckduckgo"}

    saved_ddg = brand_media.duckduckgo_image
    brand_media.duckduckgo_image = fake_ddg
    brand_media._STOCK_CACHE.pop("Brand Intelligence lens", None)
    try:
        first = asyncio.run(brand_media.stock_photo("Brand Intelligence lens"))
        second = asyncio.run(brand_media.stock_photo("Brand Intelligence lens"))
    finally:
        brand_media.duckduckgo_image = saved_ddg
        brand_media._STOCK_CACHE.pop("Brand Intelligence lens", None)

    assert first == second
    assert calls == ["Brand Intelligence lens"]  # the second call served from cache, no second search


def test_stock_photo_caches_a_miss_too_so_it_is_not_retried_forever():
    calls = []

    async def fake_ddg(query):
        calls.append(query)
        return None

    async def fake_pexels(query):
        return None

    saved_ddg, saved_pexels = brand_media.duckduckgo_image, brand_media.pexels_photo
    brand_media.duckduckgo_image, brand_media.pexels_photo = fake_ddg, fake_pexels
    brand_media._STOCK_CACHE.pop("Nonexistent Lens Nobody Photographs", None)
    try:
        first = asyncio.run(brand_media.stock_photo("Nonexistent Lens Nobody Photographs"))
        second = asyncio.run(brand_media.stock_photo("Nonexistent Lens Nobody Photographs"))
    finally:
        brand_media.duckduckgo_image, brand_media.pexels_photo = saved_ddg, saved_pexels
        brand_media._STOCK_CACHE.pop("Nonexistent Lens Nobody Photographs", None)

    assert first is None and second is None
    assert calls == ["Nonexistent Lens Nobody Photographs"]


def test_duckduckgo_image_returns_none_for_an_empty_query():
    assert asyncio.run(brand_media.duckduckgo_image("")) is None


def test_duckduckgo_image_extracts_the_first_hits_url_and_title():
    fake_module = types.ModuleType("ddgs")

    class FakeDDGS:
        def images(self, query, max_results=1, safesearch="moderate"):
            assert query == "Armor All Car care Awareness"
            return [{"image": "https://retailer.example/product.jpg", "title": "Armor All Wipes"}]

    fake_module.DDGS = FakeDDGS
    saved = sys.modules.get("ddgs")
    sys.modules["ddgs"] = fake_module
    try:
        result = asyncio.run(brand_media.duckduckgo_image("Armor All Car care Awareness"))
    finally:
        if saved is not None:
            sys.modules["ddgs"] = saved
        else:
            del sys.modules["ddgs"]

    assert result == {
        "type": "image",
        "url": "https://retailer.example/product.jpg",
        "alt": "Armor All Wipes",
        "source": "duckduckgo",
    }


def test_duckduckgo_image_returns_none_when_the_search_raises():
    fake_module = types.ModuleType("ddgs")

    class FakeDDGS:
        def images(self, query, max_results=1, safesearch="moderate"):
            raise RuntimeError("rate limited")

    fake_module.DDGS = FakeDDGS
    saved = sys.modules.get("ddgs")
    sys.modules["ddgs"] = fake_module
    try:
        result = asyncio.run(brand_media.duckduckgo_image("Armor All"))
    finally:
        if saved is not None:
            sys.modules["ddgs"] = saved
        else:
            del sys.modules["ddgs"]

    assert result is None


def test_resolve_slot_images_fills_a_banner_dict_that_never_declared_an_image_key():
    async def fake_stock_photo(query):
        return {"url": "https://images.example/x.jpg"}

    storyboard = {
        "tabs": [{"id": "t1", "label": "Issue Journey", "banner": {"eyebrow": "Issue Journey", "headline": "", "stats": []}}],
        "journey": {"banner": {"eyebrow": "Issue Journey", "headline": "", "stats": []}},
        "meta": {"issue": {"group": "x"}},  # not a banner, no image key: untouched
    }
    saved = _patch_stock_photo(fake_stock_photo)
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care"))
    finally:
        brand_media.stock_photo = saved
    assert storyboard["tabs"][0]["banner"]["image"] == "https://images.example/x.jpg"
    assert storyboard["journey"]["banner"]["image"] == "https://images.example/x.jpg"
    assert "image" not in storyboard["meta"]["issue"]


def test_slot_label_combines_the_tab_title_and_a_trimmed_banner_headline():
    label = brand_media._slot_label({"eyebrow": "Behaviour & Usage", "headline": "H" * 200})
    assert label == "Behaviour & Usage " + "H" * brand_media._HEADLINE_QUERY_CHARS


def test_profile_matches_only_when_every_byline_word_is_in_the_profile_name():
    assert brand_media.profile_matches("Kara Swisher", "Kara Swisher")
    assert brand_media.profile_matches("Kara Swisher", "Kara A. Swisher")  # middle name is fine
    assert not brand_media.profile_matches("DFB", "Daniel Farber-Ball")  # a handle is not a full name
    assert not brand_media.profile_matches("Kara Swisher", "Kara Smith")
    assert not brand_media.profile_matches("Kara Swisher", "")


def test_looks_like_person_name_rejects_handles_brands_and_anonymous():
    assert brand_media.looks_like_person_name("Kara Swisher")
    for name in ("kristyg58", "Meguiars", "Anonymous", "DFB", ""):
        assert not brand_media.looks_like_person_name(name)


def test_is_placeholder_photo_flags_muck_racks_silhouette_and_empty_urls():
    assert brand_media.is_placeholder_photo("https://cdn.muckrack.com/static/images/icon-user-circle.e3d7f0e7.png")
    assert brand_media.is_placeholder_photo(None)
    assert not brand_media.is_placeholder_photo("https://media.muckrack.com/profile/images/76/karaswisher.jpeg.128x128_q100_crop-smart.jpg")


def test_muckrack_author_photos_only_looks_up_person_like_names():
    seen = []

    def fake_batch(names):
        seen.extend(names)
        return {n: "https://media.muckrack.com/x.jpg" for n in names}

    saved = brand_media._lookup_muckrack_batch
    brand_media._lookup_muckrack_batch = fake_batch
    try:
        result = asyncio.run(brand_media.muckrack_author_photos(["Kara Swisher", "kristyg58", "Anonymous", "Kara Swisher"]))
    finally:
        brand_media._lookup_muckrack_batch = saved
    assert seen == ["Kara Swisher"]  # handles skipped, duplicate collapsed
    assert result == {"Kara Swisher": "https://media.muckrack.com/x.jpg"}


def test_muckrack_author_photos_never_raises_when_the_browser_fails():
    def broken(names):
        raise RuntimeError("no browser")

    saved = brand_media._lookup_muckrack_batch
    brand_media._lookup_muckrack_batch = broken
    try:
        assert asyncio.run(brand_media.muckrack_author_photos(["Kara Swisher"])) == {}
    finally:
        brand_media._lookup_muckrack_batch = saved


def test_muckrack_slugs_try_the_hyphenated_form_then_the_joined_one():
    assert brand_media._muckrack_slugs("Kara Swisher") == ["kara-swisher", "karaswisher"]
    assert brand_media._muckrack_slugs("Walt Mossberg") == ["walt-mossberg", "waltmossberg"]
    assert brand_media._muckrack_slugs("Cher") == ["cher"]  # one word: both forms identical
    assert brand_media._muckrack_slugs("") == []


def test_is_browser_renderable_rejects_facebook_lookaside_and_accepts_normal_cdns():
    assert not brand_media._is_browser_renderable("https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=1")
    assert not brand_media._is_browser_renderable("https://sub.lookaside.fbsbx.com/x.jpg")
    assert brand_media._is_browser_renderable("https://www.chemicalguys.com/cdn/shop/files/x.jpg")
    assert brand_media._is_browser_renderable("https://i5.walmartimages.com/seo/x.jpeg")
    assert not brand_media._is_browser_renderable("")


def test_duckduckgo_image_skips_an_unrenderable_hit_and_returns_the_next_usable_one():
    calls = []

    def fake_search():
        calls.append(1)
        return [
            {"image": "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=1", "title": "fb"},
            {"image": "https://cdn.example.com/real.jpg", "title": "real"},
        ]

    class FakeDDGS:
        def images(self, query, max_results=1, safesearch="moderate"):
            return fake_search()

    import sys
    import types

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = FakeDDGS
    saved = sys.modules.get("ddgs")
    sys.modules["ddgs"] = fake_module
    try:
        result = asyncio.run(brand_media.duckduckgo_image("Meguiar's Product Endorsement"))
    finally:
        if saved is not None:
            sys.modules["ddgs"] = saved
        else:
            del sys.modules["ddgs"]
    assert result == {"type": "image", "url": "https://cdn.example.com/real.jpg", "alt": "real", "source": "duckduckgo"}


def test_duckduckgo_image_returns_none_when_every_hit_is_unrenderable():
    import sys
    import types

    class FakeDDGS:
        def images(self, query, max_results=1, safesearch="moderate"):
            return [{"image": "https://lookaside.fbsbx.com/x", "title": "fb"}]

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = FakeDDGS
    saved = sys.modules.get("ddgs")
    sys.modules["ddgs"] = fake_module
    try:
        result = asyncio.run(brand_media.duckduckgo_image("Anything"))
    finally:
        if saved is not None:
            sys.modules["ddgs"] = saved
        else:
            del sys.modules["ddgs"]
    assert result is None
