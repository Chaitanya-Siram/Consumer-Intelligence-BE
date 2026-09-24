"""A CI build is saved after every lens, so a process killed mid-build leaves
the finished lenses on S3 and the next request builds only the rest."""
import asyncio

from consumer_intelligence import builder, router

FP = "f" * 40


def _capture_uploads(monkeypatch):
    uploads: list[str] = []
    monkeypatch.setattr(router.s3_file, "upload_file", lambda key, body: uploads.append(key))
    return uploads


def test_partial_persist_writes_latest_only_and_keeps_partial_flag(monkeypatch):
    uploads = _capture_uploads(monkeypatch)
    snapshot = {"trend_intelligence": {"meta": {"lens": "trend_intelligence"}}, "coming_soon": {}, "meta": {"lenses": ["trend_intelligence", "market_intelligence"], "built": ["trend_intelligence"], "partial": True}}
    merged = router._persist(7, None, snapshot, FP, final=False)
    assert uploads == [router._cache_key(7)]
    assert merged["meta"]["source"] == FP
    assert merged["meta"]["partial"] is True
    assert set(merged) >= {"trend_intelligence", "meta"}


def test_final_persist_writes_latest_and_a_snapshot(monkeypatch):
    uploads = _capture_uploads(monkeypatch)
    payload = {"trend_intelligence": {"meta": {}}, "coming_soon": {}, "meta": {"lenses": ["trend_intelligence"], "built": ["trend_intelligence"]}}
    merged = router._persist(7, None, payload, FP, final=True)
    assert len(uploads) == 2 and uploads[0] == router._cache_key(7)
    assert uploads[1].startswith(router._cache_prefix(7) + "/ci_charts_data_")
    assert "partial" not in merged["meta"]


def test_partial_persist_keeps_previously_cached_lenses(monkeypatch):
    _capture_uploads(monkeypatch)
    cached = {"market_intelligence": {"meta": {"lens": "market_intelligence"}}, "meta": {"source": FP, "lenses": ["market_intelligence"]}}
    snapshot = {"trend_intelligence": {"meta": {"lens": "trend_intelligence"}}, "coming_soon": {}, "meta": {"lenses": ["trend_intelligence"], "built": ["trend_intelligence"], "partial": True}}
    merged = router._persist(7, cached, snapshot, FP, final=False)
    assert "market_intelligence" in merged and "trend_intelligence" in merged
    assert merged["meta"]["lenses"] == ["market_intelligence", "trend_intelligence"]


def test_plan_builds_only_the_lenses_a_partial_cache_lacks(monkeypatch):
    partial = {
        "trend_intelligence": {"meta": {"lens": "trend_intelligence"}},
        "market_intelligence": {"meta": {"lens": "market_intelligence", "error": "boom"}, "status": "failed"},
        "meta": {"source": FP, "lenses": ["trend_intelligence", "market_intelligence", "network_map"], "partial": True},
    }
    monkeypatch.setattr(router, "_load_cache", lambda session_id: partial)
    cached, required, skip, missing = router._plan(7, [], ["trend_intelligence", "market_intelligence", "network_map"], False, FP)
    assert cached is partial
    assert skip == {"trend_intelligence"}
    assert set(missing) == {"market_intelligence", "network_map"}  # the failed lens is retried, the finished one is not


def _run_build(monkeypatch, on_lens_built, fail_lens: str | None = None):
    async def fake_build_one(lens_key, *args, **kwargs):
        if lens_key == fail_lens:
            raise RuntimeError("lens exploded")
        return {"meta": {"lens": lens_key}, "hero": {"title": lens_key}}

    async def no_qa(payload, articles, **kwargs):
        return {}

    monkeypatch.setattr(builder, "_build_one", fake_build_one)
    monkeypatch.setattr(builder.qa_agent, "run", no_qa)
    return asyncio.run(
        builder.build_ci_charts(
            workflow_nodes=None,
            requested_lenses=["trend_intelligence", "market_intelligence"],
            tagged_articles=[{"id": 1, "title": "t", "content": "c", "date": "2026-01-01", "sentiment": "POS"}],
            brand_keywords=["Amazon"],
            competitor_keywords=["Kobo"],
            with_media=False,
            on_lens_built=on_lens_built,
        )
    )


def test_builder_snapshots_grow_lens_by_lens_and_final_meta_is_not_partial(monkeypatch):
    seen: list[tuple[str, list[str], bool]] = []

    async def on_lens_built(lens_key, snapshot):
        lenses = sorted(k for k in snapshot if k in builder._MODULES)
        seen.append((lens_key, lenses, snapshot["meta"].get("partial") is True))

    out = _run_build(monkeypatch, on_lens_built)
    assert [s[0] for s in seen] == ["trend_intelligence", "market_intelligence"]
    assert seen[0][1] == ["trend_intelligence"]
    assert seen[1][1] == ["market_intelligence", "trend_intelligence"]
    assert all(s[2] for s in seen)
    assert "partial" not in out["meta"]
    assert out["meta"]["built"] == ["trend_intelligence", "market_intelligence"]


def test_builder_snapshots_include_failed_lenses_and_survive_a_failing_callback(monkeypatch):
    calls = 0

    def on_lens_built(lens_key, snapshot):  # sync callback, raises every time
        nonlocal calls
        calls += 1
        assert lens_key in snapshot
        raise OSError("s3 down")

    out = _run_build(monkeypatch, on_lens_built, fail_lens="trend_intelligence")
    assert calls == 2
    assert out["trend_intelligence"]["status"] == "failed"
    assert out["market_intelligence"]["hero"]["title"] == "market_intelligence"
