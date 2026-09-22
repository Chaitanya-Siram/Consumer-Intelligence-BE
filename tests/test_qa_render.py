"""Offline tests for the pure parts of qa_render (the browser itself needs a
running frontend). Run like tests/test_brand_hero.py."""

import base64
import json

from consumer_intelligence import qa_render

P = "http://localhost:8000/consumer-intelligence/profile-image?key="


def test_avatar_keys_and_tab_ids_read_person_rows_and_tabs():
    sb = {
        "tabs": [{"id": "overview"}, {"id": "authors"}, {"label": "no id"}],
        "authors": {"late": {"authors_by_reach": [{"name": "A", "avatar_key": "k1"}, {"name": "B"}]}},
        "editorial": {"quotes": [{"text": "hi", "author": "C", "platform": "Forums", "avatar_key": "k2"}]},
    }
    assert qa_render.avatar_keys(sb) == {"k1", "k2"}
    assert qa_render.tab_ids(sb) == ["overview", "authors"]


def test_a_drawn_picture_and_no_failures_passes():
    r = qa_render.summarise({"k1"}, [{"src": P + "k1", "ok": True}, {"src": "https://cdn.brandfetch.io/x", "ok": True}])
    assert r["status"] == "pass" and r["pictures_rendered"] == 1 and r["images_failed_to_load"] == []


def test_any_image_on_the_page_that_failed_to_load_fails_the_lens_even_a_logo():
    r = qa_render.summarise({"k1"}, [{"src": P + "k1", "ok": True}, {"src": "https://cdn.brandfetch.io/broken", "ok": False}])
    assert r["status"] == "fail" and r["images_failed_to_load"] == ["https://cdn.brandfetch.io/broken"]


def test_pictures_in_the_data_but_none_drawn_fails():
    r = qa_render.summarise({"k1", "k2"}, [{"src": "https://cdn.brandfetch.io/x", "ok": True}])
    assert r["status"] == "fail" and r["pictures_rendered"] == 0


def test_some_pictures_hidden_behind_a_control_is_a_note_not_a_failure():
    r = qa_render.summarise({"k1", "k2"}, [{"src": P + "k1", "ok": True}])
    assert r["status"] == "pass" and "1 picture(s)" in r["note"]


def test_a_lens_with_no_pictures_expected_passes_when_nothing_is_broken():
    assert qa_render.summarise(set(), [{"src": "https://x/logo.png", "ok": True}])["status"] == "pass"


def test_org_id_is_read_from_the_tokens_claims_and_bad_tokens_give_none():
    body = base64.urlsafe_b64encode(json.dumps({"org_id": 7}).encode()).decode().rstrip("=")
    assert qa_render._org_id(f"h.{body}.s") == "7"
    assert qa_render._org_id("not-a-jwt") is None


def test_check_rendering_skips_when_no_lens_carries_pictures():
    import asyncio

    r = asyncio.run(qa_render.check_rendering({"pr_research": {"tabs": []}, "brand_intelligence": {"tabs": [{"id": "t1"}]}}, frontend_url="http://x", project_id=1, session_id=1, token="t"))
    assert r["status"] == "skipped"


def test_a_quote_shown_as_a_screenshot_embed_or_preview_is_not_expected_to_draw_a_separate_avatar():
    sb = {"quotes": [
        {"text": "a", "platform": "Forums", "screenshot_key": "s", "avatar_key": "shot"},
        {"text": "b", "platform": "Twitter", "embed": {"type": "twitter"}, "avatar_key": "embed"},
        {"text": "c", "platform": "Forums", "preview": {"title": "t"}, "avatar_key": "preview"},
        {"text": "d", "platform": "Forums", "avatar_key": "card"},
    ]}
    assert qa_render.avatar_keys(sb) == {"card"}
