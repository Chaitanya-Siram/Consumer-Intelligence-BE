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
