"""Offline tests for brand_media.resolve_slot_images — the per-dimension /
per-tab sub-banner photo fill that runs alongside the lens's single
top-level hero. No test runner is configured in this repo (see CLAUDE.md);
run these the same way as tests/test_brand_hero.py.
"""

import asyncio

from consumer_intelligence import brand_media


def test_resolve_slot_images_fills_every_none_image_slot_found_anywhere():
    calls = []

    async def fake_pexels_photo(query):
        calls.append(query)
        return {"url": f"https://images.pexels.com/{query.replace(' ', '-')}.jpg"}

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
    saved = brand_media.pexels_photo
    brand_media.pexels_photo = fake_pexels_photo
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care products"))
    finally:
        brand_media.pexels_photo = saved

    assert storyboard["dimensions"][0]["image"] == "https://images.pexels.com/Armor-All-Car-care-products-Awareness.jpg"
    assert storyboard["dimensions"][1]["image"] == "https://images.pexels.com/Armor-All-Car-care-products-Trust.jpg"
    assert storyboard["tabs"][0]["banner"]["image"] is not None
    assert storyboard["hero"]["media"] is None  # untouched: no "image" key at that dict
    assert len(calls) == 3


def test_resolve_slot_images_leaves_a_slot_at_none_when_pexels_has_nothing():
    async def fake_pexels_photo(query):
        return None

    storyboard = {"dimensions": [{"name": "Advocacy", "image": None}]}
    saved = brand_media.pexels_photo
    brand_media.pexels_photo = fake_pexels_photo
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care"))
    finally:
        brand_media.pexels_photo = saved
    assert storyboard["dimensions"][0]["image"] is None


def test_resolve_slot_images_never_calls_pexels_when_no_slots_need_filling():
    calls = []

    async def fake_pexels_photo(query):
        calls.append(query)
        return {"url": "unused"}

    storyboard = {
        "dimensions": [{"name": "Trust", "image": "https://already-resolved.example/x.jpg"}],
        "meta": {"brand": "Armor All"},
    }
    saved = brand_media.pexels_photo
    brand_media.pexels_photo = fake_pexels_photo
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care"))
    finally:
        brand_media.pexels_photo = saved
    assert calls == []
    assert storyboard["dimensions"][0]["image"] == "https://already-resolved.example/x.jpg"


def test_slot_label_prefers_name_over_title_over_label_over_eyebrow():
    assert brand_media._slot_label({"name": "Awareness", "title": "x", "image": None}) == "Awareness"
    assert brand_media._slot_label({"title": "Product Recall", "eyebrow": "01 · Overview", "image": None}) == "Product Recall"
    assert brand_media._slot_label({"eyebrow": "BRAND OVERVIEW", "image": None}) == "BRAND OVERVIEW"
    assert brand_media._slot_label({"image": None}) == ""


def test_resolve_slot_images_builds_the_query_from_brand_category_and_label():
    seen = {}

    async def fake_pexels_photo(query):
        seen["query"] = query
        return None

    storyboard = {"dimensions": [{"name": "Awareness", "image": None}]}
    saved = brand_media.pexels_photo
    brand_media.pexels_photo = fake_pexels_photo
    try:
        asyncio.run(brand_media.resolve_slot_images(storyboard, "Armor All", "Car care products"))
    finally:
        brand_media.pexels_photo = saved
    assert seen["query"] == "Armor All Car care products Awareness"
