"""Offline tests for the Congruence lens's source-type icons, assistant logos,
outlet-domain logo hints and the shared journalist photo resolver."""

import asyncio

from consumer_intelligence import brand_media, logo_resolver
from consumer_intelligence.storyboard import congruence_content


def _prepared():
    def cite(llm, domain, outlet, typ, author="", title="t"):
        return {"domain": domain, "outlet": outlet, "type": typ, "author": author, "title": title}

    responses = [{"id": "ChatGPT::p1::0", "llm": "ChatGPT", "prompt_id": "p1", "prompt": "q", "text": "answer", "citations": [
        cite("ChatGPT", "thedrive.com", "The Drive", "News & trade press", "Tony Markovich"),
        cite("ChatGPT", "thedrive.com", "The Drive", "News & trade press"),
        cite("ChatGPT", "carsguide.com.au", "CarsGuide", "News & trade press"),
        cite("ChatGPT", "cnet.com", "CNET", "News & trade press"),
        cite("ChatGPT", "cars.com", "Cars.com", "News & trade press"),
        cite("ChatGPT", "reddit.com", "Reddit", "Reddit"),
    ]}]
    return {"run": {"assistants": ["ChatGPT"], "responses": responses, "prompts": [{}], "pillars": []}, "labels": {}}


def _story():
    return congruence_content.build_storyboard([], brand="Armor All", known_brands=["Armor All"], prepared=_prepared())


def test_each_source_type_lists_its_most_cited_outlets_for_the_icons():
    types = {t["name"]: t for t in _story()["analysis"]["source_types"]}
    assert types["News & trade press"]["sources"] == ["The Drive", "CarsGuide", "CNET"]  # top 3: most-cited first, ties in citation order
    assert types["Reddit"]["sources"] == ["Reddit"]


def test_the_assistants_get_a_logo_for_the_cited_by_chips():
    assert "ChatGPT" in _story()["meta"]["logos"]


def test_outlet_domains_are_exported_as_logo_hints():
    hints = _story()["meta"]["logo_domains"]
    assert hints["CarsGuide"] == "carsguide.com.au" and hints["The Drive"] == "thedrive.com"


def test_assistants_resolve_to_their_own_sites_not_a_slug_guess():
    assert brand_media._slug_domain("ChatGPT") == "chatgpt.com"
    assert brand_media._slug_domain("Claude") == "claude.ai"
    assert brand_media._slug_domain("Gemini") == "gemini.google.com"


def test_a_known_outlet_domain_is_tried_before_the_guessed_one():
    tried = []

    async def fetch(client, url):
        tried.append(url)
        return b"x" * 2000

    saved = logo_resolver._fetch_image
    logo_resolver._fetch_image = fetch
    logo_resolver._CACHE.pop("CarsGuide", None)
    logo_resolver._HASH_OWNERS.clear()
    try:
        asyncio.run(logo_resolver._resolve_one(None, "CarsGuide", [], {"CarsGuide": "carsguide.com.au"}))
    finally:
        logo_resolver._fetch_image = saved
    assert "carsguide.com.au" in tried[0]


def test_an_outlet_with_news_in_its_name_is_kept_when_its_domain_is_known():
    async def fetch(client, url):
        return b"y" * 2000

    saved = logo_resolver._fetch_image
    logo_resolver._fetch_image = fetch
    logo_resolver._HASH_OWNERS.clear()
    try:
        with_hint = asyncio.run(logo_resolver._resolve_one(None, "BBC News", [], {"BBC News": "bbc.com"}))
        without = asyncio.run(logo_resolver._resolve_one(None, "BBC News", [], None))
    finally:
        logo_resolver._fetch_image = saved
    assert with_hint and without is None


def test_journalist_photos_use_the_shared_muckrack_resolver_by_name():
    seen = {}

    async def fake(rows_by_name):
        seen.update(rows_by_name)
        for rows in rows_by_name.values():
            for r in rows:
                r["photo_url"] = "https://media.muckrack.com/x.jpg"

    saved = brand_media.resolve_muckrack_photos
    brand_media.resolve_muckrack_photos = fake
    try:
        sb = {"analysis": {"journalists": [{"name": "Tony Markovich"}, {"name": "Brian Cooley"}]}}
        asyncio.run(congruence_content.resolve_journalist_photos(sb))
    finally:
        brand_media.resolve_muckrack_photos = saved
    assert set(seen) == {"Tony Markovich", "Brian Cooley"}
    assert all(j["photo_url"] for j in sb["analysis"]["journalists"])


def test_muckrack_resolver_skips_handles_serves_cache_and_stores_new_finds():
    saved = (brand_media.load_author_photo_cache, brand_media.save_author_photo_cache, brand_media.muckrack_author_photos)
    stored = {}

    async def fetch(names):
        assert names == ["Brian Cooley"]  # the cached one and the handle are not looked up
        return {"Brian Cooley": "https://media.muckrack.com/bc.jpg"}

    brand_media.load_author_photo_cache = lambda: {"Tony Markovich": "https://media.muckrack.com/tm.jpg"}
    brand_media.save_author_photo_cache = lambda c: stored.update(c)
    brand_media.muckrack_author_photos = fetch
    rows = {"Tony Markovich": [{}], "Brian Cooley": [{}], "kristyg58": [{}]}
    try:
        asyncio.run(brand_media.resolve_muckrack_photos(rows))
    finally:
        brand_media.load_author_photo_cache, brand_media.save_author_photo_cache, brand_media.muckrack_author_photos = saved
    assert rows["Tony Markovich"][0]["photo_url"].endswith("tm.jpg")
    assert rows["Brian Cooley"][0]["photo_url"].endswith("bc.jpg")
    assert "photo_url" not in rows["kristyg58"][0]
    assert stored["Brian Cooley"].endswith("bc.jpg")


def test_a_multi_author_byline_is_split_into_separate_journalists():
    assert congruence_content._split_bylines("Collin Morgan, Brian Silvestro") == ["Collin Morgan", "Brian Silvestro"]
    assert congruence_content._split_bylines("Ann Lee and Bo Chen") == ["Ann Lee", "Bo Chen"]
    assert congruence_content._split_bylines("Ann Lee & Bo Chen; Cy Dunn Jr") == ["Ann Lee", "Bo Chen", "Cy Dunn Jr"]


def test_a_last_comma_first_byline_or_single_name_is_left_whole():
    assert congruence_content._split_bylines("Morgan, Collin") == ["Morgan, Collin"]
    assert congruence_content._split_bylines("Tony Markovich") == ["Tony Markovich"]
    assert congruence_content._split_bylines("") == []


def test_each_author_of_a_shared_byline_is_counted_as_a_journalist():
    prepared = _prepared()
    prepared["run"]["responses"][0]["citations"] = [
        {"domain": "caranddriver.com", "outlet": "Car and Driver", "type": "News & trade press", "author": "Collin Morgan, Brian Silvestro", "title": "t"},
    ]
    names = {j["name"] for j in congruence_content.build_storyboard([], brand="Armor All", known_brands=["Armor All"], prepared=prepared)["analysis"]["journalists"]}
    assert names == {"Collin Morgan", "Brian Silvestro"}


def test_generic_newsroom_bylines_are_not_journalists():
    for name in ("Autoblog Staff", "The Editors", "Editorial Team", "Guest Contributor", "News Desk"):
        assert congruence_content._is_generic_byline(name), name
    for name in ("Tony Markovich", "Brian Cooley", "A.J. Baime"):
        assert not congruence_content._is_generic_byline(name), name


def test_a_type_outside_the_top_ranked_sources_still_gets_icons_and_logo_hints():
    prepared = _prepared()
    cites = [{"domain": f"site{i}.com", "outlet": f"Site {i}", "type": "News & trade press", "author": "", "title": "t"} for i in range(12)]
    cites += [{"domain": "apnews.com", "outlet": "AP News", "type": "News wires", "author": "", "title": "t"}]
    prepared["run"]["responses"][0]["citations"] = cites
    sb = congruence_content.build_storyboard([], brand="Armor All", known_brands=["Armor All"], prepared=prepared)
    wires = next(t for t in sb["analysis"]["source_types"] if t["name"] == "News wires")
    assert wires["sources"] == ["AP News"]  # one citation: nowhere near the top 10 sources, still iconed
    assert sb["meta"]["logo_domains"]["AP News"] == "apnews.com" and "AP News" in sb["meta"]["logos"]
