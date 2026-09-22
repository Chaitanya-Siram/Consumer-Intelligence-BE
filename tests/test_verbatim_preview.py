from consumer_intelligence.verbatim_capture import parse_preview
from consumer_intelligence import aggregate, cohorts, quotes


def test_parse_preview_reads_open_graph_and_resolves_relative_image():
    html = (
        '<html><head><title>fallback</title>'
        '<meta property="og:title" content="Best &amp; cheapest wax">'
        '<meta property="og:description" content="Used it on my Porsche.">'
        '<meta property="og:image" content="/img/a.jpg">'
        '<meta property="og:site_name" content="Instagram"></head></html>'
    )
    p = parse_preview(html, "https://www.instagram.com/p/abc/")
    assert p["title"] == "Best & cheapest wax"
    assert p["image"] == "https://www.instagram.com/img/a.jpg"
    assert p["site"] == "Instagram"
    assert "instagram.com" in p["favicon"]


def test_parse_preview_falls_back_to_title_tag():
    p = parse_preview("<title>Only a title</title>", "https://forum.example.com/t/1")
    assert p["title"] == "Only a title"
    assert p["site"] == "forum.example.com"


def test_parse_preview_returns_none_for_empty_page():
    assert parse_preview("<html><body>hi</body></html>", "https://x.com/") is None


def test_platform_prefers_content_source_name_over_section():
    article = {"content source name": "Instagram", "section": "Industry News", "url": "https://instagram.com/p/1"}
    assert cohorts.platform(article) == "Instagram"
    assert aggregate.source_platform(article) == "Instagram"
    assert quotes.source(article).startswith("Instagram")


def test_quote_uses_the_posters_full_text_not_the_llm_summary():
    article = {"content": "", "full text": "I love Armor All wipes. Best I have used.", "summary": "A user praises a cleaning product."}
    assert quotes.snippet(article, ["armor all"]).startswith("I love Armor All wipes")


def test_quote_falls_back_to_summary_when_there_is_no_body():
    assert quotes.snippet({"content": "", "summary": "A short summary of the post here, long enough."}).startswith("A short summary")


def test_preview_of_a_login_wall_is_rejected():
    import asyncio
    from consumer_intelligence import verbatim_capture as vc

    class Resp:
        status_code, url = 200, "https://www.amazon.com/ap/signin"
        text = "<html><head><title>Sign in</title><meta property='og:description' content='x'></head></html>"

    class Client:
        async def get(self, *a, **k):
            return Resp()

    assert asyncio.run(vc._fetch_preview("https://www.amazon.com/p/1", Client())) is None


def test_product_query_strips_the_brand_and_adds_the_category():
    from consumer_intelligence.product_images import product_query

    assert product_query("Armor All Extreme Tire Shine Spray", "Armor All", "Car care products") == "Extreme Tire Shine Spray Car care"
    assert product_query("Armor All", "Armor All", "Car care") is None
    assert product_query("Turtle Wax", "Turtle Wax") is None


def _qa_payload():
    quote_ok = {"text": "I love Armor All wipes.", "source": "Review · amazon.com", "url": "https://shop.example.com/p/1", "platform": "Review", "preview": {"title": "t"}}
    paraphrase = {"text": "A user praises a cleaning product.", "source": "Forums · x.example.com", "url": "https://forum.example.com/t/9", "platform": "Forums", "preview": {"title": "t"}}
    bad_link = {"text": "Best wax ever made.", "source": "Blogs · b.example.com", "url": "javascript:alert(1)", "platform": "Blogs", "preview": {"title": "t"}}
    no_facts = {"text": "Great shine on my truck.", "source": "Twitter · t.example.com", "url": "https://t.example.com/a/status/1"}
    return {
        "meta": {"brand": "Armor All", "competitors": []},
        "social_listening": {
            "meta": {"logos": {"Armor All": "u1", "Industry News": "u2"}, "brand": "Armor All", "competitors": []},
            "overall": {"quotes": [quote_ok, paraphrase, bad_link, no_facts]},
        },
    }


def _qa_articles():
    return [
        {"url": "https://shop.example.com/p/1", "full text": "I love Armor All wipes. Really."},
        {"url": "https://forum.example.com/t/9", "full text": "Honestly the Armor All protectant is the best I have used on vinyl.", "summary": "A user praises a cleaning product."},
        {"url": "https://blog.example.com/w", "full text": "Best wax ever made. Truly."},
        {"url": "https://t.example.com/a/status/1", "full text": "Great shine on my truck. Thanks."},
    ]


def test_qa_agent_repairs_text_link_card_facts_and_junk_logo_then_reverifies():
    import asyncio
    from consumer_intelligence import qa_agent

    payload = _qa_payload()
    report = asyncio.run(qa_agent.run(payload, _qa_articles(), max_iterations=3, network=False))
    sl = payload["social_listening"]
    qs = sl["overall"]["quotes"]
    assert "Industry News" not in sl["meta"]["logos"] and "Armor All" in sl["meta"]["logos"]
    assert qs[1]["text"].startswith("Honestly the Armor All protectant")  # re-cut from the poster's own words
    assert qs[2]["url"] == "https://blog.example.com/w"  # unsafe link replaced by the article's own
    assert qs[3]["platform"] == "Twitter"
    assert report["iterations"] >= 2 and report["remaining_count"] == 0
    assert payload["meta"]["qa"] is report
    assert {"logo_junk", "not_verbatim", "bad_url", "no_card_facts"} <= set(report["fixed"])


def test_qa_agent_can_be_switched_off():
    import asyncio
    from consumer_intelligence import qa_agent

    assert asyncio.run(qa_agent.run({"meta": {}}, [], max_iterations=0)) == {"skipped": True}


def test_evidence_level_orders_embed_over_screenshot_over_preview_over_card():
    from consumer_intelligence.qa_agent import evidence_level

    assert [evidence_level(q) for q in ({"embed": {"type": "twitter"}}, {"screenshot_key": "k"}, {"preview": {"t": 1}}, {"platform": "X"}, {})] == [4, 3, 2, 1, 0]


def test_evidence_issues_are_repaired_in_one_batched_capture_pass(monkeypatch):
    import asyncio
    from consumer_intelligence import qa_agent, verbatim_capture

    calls = []

    async def fake_resolve_evidence(quotes):
        calls.append(len(quotes))
        for q in quotes:
            q["preview"] = {"title": "recovered"}
        return quotes

    monkeypatch.setattr(verbatim_capture, "resolve_evidence", fake_resolve_evidence)
    quotes = [{"text": f"Quote {i} words here.", "url": f"https://x.example.com/{i}", "platform": "Forums"} for i in range(4)]
    payload = {"meta": {"brand": "Armor All", "competitors": []}, "l": {"meta": {"logos": {}}, "s": {"quotes": quotes}}}
    report = asyncio.run(qa_agent.run(payload, [], max_iterations=2, network=True))
    assert calls == [4]  # one capture call for all four quotes, not four separate ones
    assert all(q.get("preview") for q in quotes)
    assert report["fixed"].get("evidence_upgradable") == 4


def test_walk_media_slots_finds_banner_and_hero_images_but_skips_products_and_video():
    from consumer_intelligence.qa_agent import _walk_media_slots

    storyboard = {
        "hero": {"media": {"type": "image", "url": "https://x.example.com/hero.jpg"}},
        "dimensions": [{"key": "trust", "image": "https://x.example.com/trust.jpg"}],
        "tabs": [{"banner": {"title": "T1", "image": None}}],  # unresolved slot: not yet a candidate
        "leaders": {"items": [{"media": {"type": "youtube", "url": "https://youtube.com/embed/1"}}]},  # video, skipped
        "perception": {"products": [{"name": "Wipes", "image": {"url": "https://x.example.com/product.jpg"}}]},  # dict shape, skipped
    }
    found = [(shape, url) for shape, _container, url in _walk_media_slots(storyboard)]
    assert ("media", "https://x.example.com/hero.jpg") in found
    assert ("image", "https://x.example.com/trust.jpg") in found
    assert len(found) == 2  # the None banner, the youtube media and the product dict are all excluded


def test_qa_agent_repairs_a_broken_dimension_image_by_re_resolving_it(monkeypatch):
    import asyncio

    from consumer_intelligence import brand_media, logo_resolver, qa_agent

    async def fake_fetch_image(client, url):
        return None if url == "https://dead.example.com/x.jpg" else b"bytes"

    async def fake_stock_photo(query):
        assert query == "Armor All Car care Trust"
        return {"type": "image", "url": "https://live.example.com/new.jpg", "source": "duckduckgo"}

    monkeypatch.setattr(logo_resolver, "_fetch_image", fake_fetch_image)
    monkeypatch.setattr(brand_media, "stock_photo", fake_stock_photo)
    payload = {
        "meta": {"brand": "Armor All"},
        "brand_health_storyboard": {
            "meta": {"brand": "Armor All", "category": "Car care", "logos": {"Armor All": "https://logo.example/armor-all.png"}},
            "dimensions": [{"key": "trust", "name": "Trust", "image": "https://dead.example.com/x.jpg"}],
        },
    }
    report = asyncio.run(qa_agent.run(payload, [], max_iterations=2, network=True))
    assert payload["brand_health_storyboard"]["dimensions"][0]["image"] == "https://live.example.com/new.jpg"
    assert report["fixed"].get("slot_image_broken") == 1
    assert report["remaining_count"] == 0


def test_qa_agent_clears_a_broken_hero_media_when_nothing_live_is_found(monkeypatch):
    import asyncio

    from consumer_intelligence import brand_media, logo_resolver, qa_agent

    async def fake_fetch_image(client, url):
        return None  # every candidate is dead, including whatever a repair might try

    async def fake_stock_photo(query):
        return None  # DuckDuckGo and Pexels both come up empty

    monkeypatch.setattr(logo_resolver, "_fetch_image", fake_fetch_image)
    monkeypatch.setattr(brand_media, "stock_photo", fake_stock_photo)
    payload = {
        "meta": {"brand": "Armor All"},
        "brand_health_storyboard": {
            "meta": {"brand": "Armor All", "logos": {}},
            "hero": {"media": {"type": "image", "url": "https://dead.example.com/hero.jpg"}},
        },
    }
    asyncio.run(qa_agent.run(payload, [], max_iterations=2, network=True))
    assert payload["brand_health_storyboard"]["hero"]["media"] is None  # cleared, not left dangling


def test_run_stops_within_its_time_budget_instead_of_hanging(monkeypatch):
    import asyncio
    from consumer_intelligence import qa_agent, verbatim_capture

    async def slow_resolve_evidence(quotes):
        await asyncio.sleep(0.2)
        return quotes

    monkeypatch.setattr(verbatim_capture, "resolve_evidence", slow_resolve_evidence)
    monkeypatch.setattr(qa_agent, "_time_budget", lambda: 0.001)
    quotes = [{"text": "Quote words here.", "url": "https://x.example.com/1", "platform": "Forums"}]
    payload = {"meta": {"brand": "Armor All", "competitors": []}, "l": {"meta": {"logos": {}}, "s": {"quotes": quotes}}}
    report = asyncio.run(qa_agent.run(payload, [], max_iterations=5, network=False))
    assert report["timed_out"] is True
    assert report["iterations"] == 0


def test_discover_brand_names_finds_every_brand_key_in_a_ranking_table():
    from consumer_intelligence.logo_resolver import discover_brand_names

    storyboard = {
        "meta": {"brand": "Armor All", "logos": {}},
        "competitive": {"rows": [{"brand": "NXT Wax", "share_of_voice": 0.9}, {"brand": "303", "share_of_voice": 0.3}]},
        "themes": [{"name": "Gift Guide"}],  # "name" key must NOT be picked up
    }
    assert discover_brand_names(storyboard) == {"Armor All", "NXT Wax", "303"}
