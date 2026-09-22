"""Brand Perception's "What people say" carries real posts, and the Create
Dashboard build resolves their evidence like every other verbatim section."""

from consumer_intelligence import builder, profile_images, qa_agent
from consumer_intelligence.storyboard import brand_perception


def _articles(n=10):
    return [
        {
            "id": f"A{i}", "url": f"https://forum.example/t/{i}", "title": f"Thread {i}", "author": f"poster{i}",
            "content source name": "Forums", "date": "2023-08-01", "sentiment": "Positive",
            "content": f"Switched to Armor All after my old wax number {i} failed. It was a much better product overall and lasted.",
        }
        for i in range(n)
    ]


def _prepared(articles):
    labels = {a["id"]: {"switching": True, "driver": "quality", "product": ""} for a in articles}
    return {"labels": {"by_id": labels}}


def test_switching_posts_carry_the_link_platform_author_and_date_and_more_of_them():
    arts = _articles(10)
    try:
        sb = brand_perception.build_storyboard(arts, brand="Armor All", known_brands=["Armor All"], prepared=_prepared(arts))
    except Exception as exc:  # the prepared-labels shape is the module's own; skip if the fixture drifts
        raise AssertionError(f"fixture no longer matches brand_perception's prepared shape: {exc}")
    posts = sb["switching"]["posts"]
    assert len(posts) == brand_perception.MAX_POSTS == 8
    first = posts[0]
    assert first["url"].startswith("https://forum.example/t/") and first["platform"] == "Forums"
    assert first["author"].startswith("poster") and first["date"] == "2023-08-01" and first["title"].startswith("Thread")


def test_the_build_resolves_evidence_for_the_switching_posts():
    assert builder._VERBATIM_SECTIONS[brand_perception.LENS_KEY] == ("switching",)
    assert "posts" in builder._QUOTE_KEYS


def test_switching_posts_get_profile_pictures_and_are_checked_by_the_quality_agent():
    assert profile_images._PEOPLE_LISTS["posts"] == "author"
    sb = {"switching": {"posts": [{"text": "hello there world", "author": "a", "url": "https://x/1", "platform": "Forums"}]}}
    assert [q["author"] for q in qa_agent._walk_quotes(sb)] == ["a"]
