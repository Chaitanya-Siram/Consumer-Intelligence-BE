"""The CI charts cache is keyed by session id only; the fingerprint makes sure a
payload is reused only for the data it was built from."""
from types import SimpleNamespace

from consumer_intelligence import router


def _record(**kw):
    base = {"created_at": "2026-09-22 09:51:32", "brand_keywords": ["Amazon"], "competitor_keywords": ["Kindle"]}
    base.update(kw)
    return SimpleNamespace(**base)


ARTICLES = [{"id": 1, "is_approved_for_dashboards": True}, {"id": 2, "is_approved_for_dashboards": False}]


def test_fingerprint_is_stable_and_order_independent():
    a = router._fingerprint(_record(), ARTICLES)
    b = router._fingerprint(_record(), list(reversed(ARTICLES)))
    assert a == b and len(a) == 40


def test_fingerprint_changes_with_articles_brand_or_session():
    base = router._fingerprint(_record(), ARTICLES)
    assert router._fingerprint(_record(), ARTICLES + [{"id": 3}]) != base
    assert router._fingerprint(_record(brand_keywords=["Armor All"]), ARTICLES) != base
    assert router._fingerprint(_record(created_at="2026-09-01 00:00:00"), ARTICLES) != base
    approved = [dict(a, is_approved_for_dashboards=True) for a in ARTICLES]
    assert router._fingerprint(_record(), approved) != base


def test_cache_from_other_data_is_rejected():
    fp = router._fingerprint(_record(), ARTICLES)
    same = {"meta": {"source": fp, "lenses": ["trend_intelligence"]}, "trend_intelligence": {"meta": {}}}
    other = {"meta": {"source": "deadbeef", "lenses": ["trend_intelligence"]}, "trend_intelligence": {"meta": {}}}
    legacy = {"meta": {"lenses": ["trend_intelligence"]}, "trend_intelligence": {"meta": {}}}
    assert router._cache_matches(same, fp, 10) is same
    assert router._cache_matches(other, fp, 10) is None
    assert router._cache_matches(legacy, fp, 10) is None
    assert router._cache_matches(None, fp, 10) is None


def test_stamp_source_writes_meta():
    payload = {"meta": {"lenses": []}}
    router._stamp_source(payload, "abc")
    assert payload["meta"]["source"] == "abc"
    bare = {}
    router._stamp_source(bare, "abc")
    assert bare["meta"]["source"] == "abc"
