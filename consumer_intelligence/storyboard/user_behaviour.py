"""User Behaviour Analysis storyboard — Consumer Segmentation Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-user-behaviour.md
Screen:   Consumer-Intelligence-FE/src/screens/UserBehaviourScreen.jsx

Segment shares come from LLM life-stage inference with a confidence gate
(behaviour_classify.prepare); when fewer than 20% of posts get a band the
`pct` fields are omitted and the note says so. brand_choice reuses
brands.mention_counts (the brand_competitive_intel tally) over multi-product
posts. Question bullets are grounded in reason / rule clusters of >= 3 posts.
"""

from .. import aggregate, brand_media, cohorts, timeseries
from ..behaviour_classify import MIN_POSTS_PER_POINT, SEGMENT_COVERAGE, label_of, prepare
from . import brands as brand_metrics

LENS_KEY = "user_behaviour"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "t1", "label": "Audience Segments"},
    {"id": "t2", "label": "Multiple-Card Behaviour"},
]
QUESTIONS = [
    ("why", "Why do they use more than one brand or product?"),
    ("how", "How do they choose between the ones they have?"),
]
MAX_BRANDS = 4
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _clusters(rows: list[dict], merged: dict, field: str, prepared: dict) -> list[dict]:
    mapping, titles = merged.get("map") or {}, {t["key"]: t["title"] for t in merged.get("themes", [])}
    by_key: dict[str, list[dict]] = {}
    for a in rows:
        lab = label_of(a, prepared)[field]
        k = mapping.get(lab) if lab else None
        if k:
            by_key.setdefault(k, []).append(a)
    out = [{"key": k, "title": titles.get(k, k.replace("-", " ").title()), "count": len(v), "sample_posts": [_brief(a) for a in v[:SAMPLE]]} for k, v in by_key.items() if len(v) >= MIN_POSTS_PER_POINT]
    out.sort(key=lambda c: -c["count"])
    return out[:4]


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    # ── tab 1: segments ───────────────────────────────────────────────────
    seg_meta = prepared.get("segments") or {}
    smap = seg_meta.get("map") or {}
    by_seg: dict[str, list[dict]] = {}
    for a in articles:
        lab = label_of(a, prepared)["segment"]
        k = smap.get(lab) if lab else None
        if k:
            by_seg.setdefault(k, []).append(a)
    # Segments below the evidence floor are dropped before shares are computed,
    # so the shown groups still sum to 100.
    by_seg = {k: v for k, v in by_seg.items() if len(v) >= MIN_POSTS_PER_POINT}
    banded = sum(len(v) for v in by_seg.values())
    coverage = banded / total if total else 0.0
    shares_ok = coverage >= SEGMENT_COVERAGE and banded > 0
    pct_rows = cohorts.pct_rows({k: len(v) for k, v in by_seg.items()}) if shares_ok else []
    pct_of = {r["name"]: r["pct"] for r in pct_rows}
    groups = []
    for t in seg_meta.get("themes", []):
        # A segment needs the same evidence floor as a bullet: >= 3 posts.
        if t["key"] not in by_seg or len(by_seg[t["key"]]) < MIN_POSTS_PER_POINT:
            continue
        g = {"key": t["key"], "range": t.get("range") or "", "title": t["title"], "count": len(by_seg[t["key"]]), "points": []}
        if shares_ok:
            g["pct"] = pct_of.get(t["key"], 0)
        groups.append(g)
    groups.sort(key=lambda g: (g["range"], -g["count"]))
    largest = max(groups, key=lambda g: g["count"]) if groups else None
    segments = {
        "banner": {
            "eyebrow": "Decision making process & selection cycle",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(len(groups)), "label": "Sub-segments"},
                {"value": (largest["range"] or largest["title"]) if largest else "—", "label": "Largest segment"},
                {"value": f"{largest['pct']}%" if largest and "pct" in largest else f"{banded} posts", "label": f"Share of posts · {largest['range'] or largest['title']}" if largest and "pct" in largest else "Posts with a segment cue"},
                {"value": f"{total:,}", "label": "Posts analysed"},
            ],
        },
        "note": "" if shares_ok else f"Segment shares are not shown: only {banded} of {total} posts ({round(coverage * 100)}%) carry a clear audience cue.",
        "lead": "",
        "groups": groups,
        "coverage_pct": round(coverage * 100),
        "shares_shown": shares_ok,
    }

    # ── tab 2: multiple-product behaviour ─────────────────────────────────
    multi_rows = [a for a in articles if label_of(a, prepared)["multi"]]
    tally = brand_metrics.mention_counts(multi_rows, known)
    ranked = [n for n, _ in aggregate.top_n(tally, len(tally))]
    keep, tail = ranked[:MAX_BRANDS], ranked[MAX_BRANDS:]
    choice_counts = {n: tally[n] for n in keep}
    if tail:
        choice_counts["Others"] = sum(tally[n] for n in tail)
    order = keep + (["Others"] if tail else [])
    brand_choice = []
    for r in (cohorts.pct_rows(choice_counts, order=order) if choice_counts else []):
        row = {"name": r["name"], "pct": r["pct"]}
        if brand and r["name"].lower() == brand.lower():
            row["is_brand"] = True
        brand_choice.append(row)
    reasons = _clusters(multi_rows, prepared.get("reasons") or {}, "reason", prepared)
    rules = _clusters(multi_rows, prepared.get("rules") or {}, "rule", prepared)
    brand_share = next((r["pct"] for r in brand_choice if r.get("is_brand")), None)
    multi = {
        "banner": {
            "eyebrow": "Multiple-product behaviour",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{brand_share}%" if brand_share is not None else "—", "label": f"{brand} · brand choice" if brand else "Brand choice"},
                {"value": str(len(tally)), "label": "Brands tracked"},
                {"value": str(len(reasons)), "label": "Reasons to use more"},
                {"value": str(len(rules)), "label": "Ways they choose"},
            ],
        },
        "note": "",
        "brand_choice": brand_choice,
        "questions": [{"key": key, "q": q, "points": []} for key, q in QUESTIONS],
        "multi_posts": len(multi_rows),
    }

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos([brand, *keep], articles),
            "method": {
                "segments": f"LLM life-stage inference per post, confidence >= 0.6; shares shown only when >= {int(SEGMENT_COVERAGE * 100)}% of posts are banded",
                "brand_choice": "brands.mention_counts (the brand_competitive_intel tally) over multi-product posts; top 4 + Others normalised to 100",
                "questions": f"bullets only for reason/rule clusters backed by >= {MIN_POSTS_PER_POINT} posts",
            },
            "classification": {
                "labels": (prepared.get("labels") or {}).get("method"),
                "segments": seg_meta.get("method"),
                "segment_coverage_pct": round(coverage * 100),
                "segment_bands": [{"key": t["key"], "title": t["title"], "range": t.get("range"), "raw": t["raw"][:8]} for t in seg_meta.get("themes", [])],
                "multi_posts": len(multi_rows),
                "reason_clusters": [{"key": c["key"], "count": c["count"]} for c in reasons],
                "rule_clusters": [{"key": c["key"], "count": c["count"]} for c in rules],
            },
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["User Behaviour Analysis · Consumer Segmentation Analysis", f"Computed from {total:,} tagged posts"],
        "segments": segments,
        "multi": multi,
        "evidence": {
            "segments": {g["key"]: [_brief(a) for a in by_seg[g["key"]][:SAMPLE]] for g in groups},
            "reasons": reasons,
            "rules": rules,
            "multi_sample": [_brief(a) for a in multi_rows[:SAMPLE]],
        },
    }
