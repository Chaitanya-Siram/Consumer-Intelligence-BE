"""Offline tests for post_avatar.find_avatar and the forum/review branch of
profile_images. Run like tests/test_brand_hero.py (no test runner configured)."""

from consumer_intelligence import post_avatar, profile_images

XENFORO = """
<article class="message"><div class="message-user">
  <a class="avatar avatar--m" href="/members/joebruin77.1/"><img src="/data/avatars/l/93/93195.jpg" alt="joebruin77"></a>
  <h4 class="message-name"><a class="username" href="/members/joebruin77.1/">joebruin77</a></h4>
</div><div class="message-body">Use a two bucket wash.</div></article>
<article class="message"><div class="message-user">
  <a class="avatar avatar--m"><img src="/data/avatars/l/1/1.jpg" alt="someoneelse"></a>
  <h4 class="message-name"><a class="username">someoneelse</a></h4></div></article>
"""

VBULLETIN_BLOCK = """
<table class="post"><tr><td class="alt2">
  <a class="bigusername">Davesrb</a>
  <div><img class="avatar" src="customavatars/avatar123_4.gif"></div>
</td><td>Great wax.</td></tr></table>
<table class="post"><tr><td class="alt2">
  <a class="bigusername">Other</a><div><img class="avatar" src="customavatars/avatar9_1.gif"></div>
</td></tr></table>
"""

LAZY = '<div class="post"><b>Hawkeye</b><img class="avatar lazyload" src="data:image/gif;base64,R0lGOD" data-src="/img/hawkeye.png"></div>'
AMBIGUOUS = '<div class="thread"><b>Riggs</b><img class="avatar" src="/a1.jpg"><b>Other</b><img class="avatar" src="/a2.jpg"></div>'
DEFAULT_ONLY = '<div class="post"><b>Newbie</b><img class="avatar" src="/images/default_avatar.png"></div>'
BADGE = '<div class="post"><b>Wayne R</b><img class="avatar" src="https://cdn.example.com/10_Proficient.svg"></div>'


def test_avatar_whose_alt_names_the_author_is_found_and_made_absolute():
    assert post_avatar.find_avatar(XENFORO, "joebruin77", "https://tmc.example/threads/x/") == "https://tmc.example/data/avatars/l/93/93195.jpg"


def test_the_single_avatar_in_the_authors_own_block_is_used():
    assert post_avatar.find_avatar(VBULLETIN_BLOCK, "Davesrb", "https://forum.example/showthread.php") == "https://forum.example/customavatars/avatar123_4.gif"


def test_a_lazy_loaded_avatar_uses_its_data_src_not_the_placeholder_uri():
    assert post_avatar.find_avatar(LAZY, "Hawkeye", "https://forum.example/") == "https://forum.example/img/hawkeye.png"


def test_a_block_holding_several_avatars_is_ambiguous_so_nothing_is_returned():
    assert post_avatar.find_avatar(AMBIGUOUS, "Riggs", "https://forum.example/") is None


def test_default_avatars_and_svg_rank_badges_are_rejected():
    assert post_avatar.find_avatar(DEFAULT_ONLY, "Newbie", "https://forum.example/") is None
    assert post_avatar.find_avatar(BADGE, "Wayne R", "https://forum.example/") is None


def test_unknown_author_or_empty_page_yields_nothing():
    assert post_avatar.find_avatar(XENFORO, "nobody", "https://x/") is None
    assert post_avatar.find_avatar("", "joebruin77", "https://x/") is None


def test_forum_and_review_posts_are_identified_by_author_and_host():
    forum = {"url": "https://teslamotorsclub.com/tmc/threads/x.1/#post-2", "author": "joebruin77", "content source name": "Forums"}
    assert profile_images.identify(forum) == ("post", "joebruin77@teslamotorsclub.com")
    review = {"url": "https://www.acehardware.com/p/1", "author": "rachelb640", "content source name": "Review"}
    assert profile_images.identify(review) == ("post", "rachelb640@www.acehardware.com")
    assert profile_images.identify({"url": "https://blog.example.com/a", "author": "Jane Doe", "content source name": "Blogs"}) is None
    assert profile_images.identify({"url": "https://forum.example.com/t/1", "author": "", "content source name": "Forums"}) is None


def test_a_picture_given_to_several_posters_is_a_site_default_and_is_taken_back():
    profile_images._SOURCE_URL.update({"k1": "https://site/avatar-150.png", "k2": "https://site/avatar-150.png", "k3": "https://site/u/3.jpg"})
    rows = [{"avatar_key": "k1"}, {"avatar_key": "k2"}, {"avatar_key": "k3"}, {"name": "no avatar"}]
    try:
        profile_images._drop_shared_defaults(rows)
    finally:
        for k in ("k1", "k2", "k3"):
            profile_images._SOURCE_URL.pop(k, None)
    assert rows == [{}, {}, {"avatar_key": "k3"}, {"name": "no avatar"}]


# ── quality agent: profile pictures ────────────────────────────────────────

import asyncio

from consumer_intelligence import qa_agent

_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
_ARTICLES = [{"url": "https://forum.example/t/1", "author": "Klasse Act", "content source name": "Forums"}]


def _storyboard(**row):
    return {"authors": {"late": {"authors_by_reach": [{"name": "Klasse Act", **row}]}}}


class _Fakes:
    """Swap the S3 read and the resolver for the duration of a QA run."""

    def __init__(self, stored: dict, resolved: str | None):
        self.stored, self.resolved = stored, resolved

    def __enter__(self):
        class S3:
            def download_file(_, key):
                if key not in self.stored:
                    raise KeyError(key)
                return self.stored[key]

        async def resolve(provider, handle):
            return self.resolved

        self.saved = (qa_agent.s3_file, profile_images._resolve, dict(profile_images._RESOLVED))
        qa_agent.s3_file, profile_images._resolve = S3(), resolve
        profile_images._RESOLVED.clear()
        return self

    def __exit__(self, *exc):
        qa_agent.s3_file, profile_images._resolve = self.saved[0], self.saved[1]
        profile_images._RESOLVED.clear()
        profile_images._RESOLVED.update(self.saved[2])


def _run(payload):
    return asyncio.run(qa_agent.run(payload, _ARTICLES, max_iterations=2, network=True))


def test_is_image_bytes_accepts_real_images_and_rejects_empty_and_html_pages():
    assert profile_images.is_image_bytes(_PNG)
    assert profile_images.is_image_bytes(b"\xff\xd8\xff" + b"x" * 2000)
    assert not profile_images.is_image_bytes(b"")
    assert not profile_images.is_image_bytes(b"<html>" + b"x" * 2000)
    assert not profile_images.is_image_bytes(b"\x89PNG")  # truncated


def test_a_working_profile_picture_raises_no_issue():
    payload = {"pr_research": _storyboard(avatar_key="k1")}
    with _Fakes({"k1": _PNG}, None):
        report = _run(payload)
    assert report["remaining_count"] == 0 and payload["pr_research"]["authors"]["late"]["authors_by_reach"][0]["avatar_key"] == "k1"


def test_a_profile_picture_the_endpoint_cannot_serve_is_dropped_and_reported_fixed():
    payload = {"pr_research": _storyboard(avatar_key="gone")}
    with _Fakes({"gone": b"<html>not an image</html>"}, None):
        report = _run(payload)
    assert "avatar_key" not in payload["pr_research"]["authors"]["late"]["authors_by_reach"][0]
    assert report["fixed"].get("avatar_broken") == 1


def test_a_broken_picture_is_replaced_by_a_fresh_one_when_the_poster_can_be_refetched():
    payload = {"pr_research": _storyboard(avatar_key="gone")}
    with _Fakes({"fresh": _PNG}, "fresh"):
        _run(payload)
    assert payload["pr_research"]["authors"]["late"]["authors_by_reach"][0]["avatar_key"] == "fresh"


def test_an_author_row_with_a_findable_poster_but_no_picture_gets_one():
    payload = {"pr_research": _storyboard()}
    with _Fakes({"k2": _PNG}, "k2"):
        report = _run(payload)
    assert payload["pr_research"]["authors"]["late"]["authors_by_reach"][0]["avatar_key"] == "k2"
    assert report["fixed"].get("avatar_missing") == 1


def test_a_poster_already_known_to_have_no_picture_is_not_flagged_as_a_defect():
    payload = {"pr_research": _storyboard()}
    with _Fakes({}, None):
        profile_images._RESOLVED[("post", "klasse act@forum.example")] = None
        report = _run(payload)
    assert report["remaining_count"] == 0
