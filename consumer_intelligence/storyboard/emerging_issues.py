"""Track Emerging Issues storyboard — Issues Intelligence Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-issues-priorities.md §4.
Screen:   Consumer-Intelligence-FE/src/screens/TrackEmergingIssuesScreen.jsx

Every number here is computed from the session's tagged articles. Every prose
field is left empty for narrative.py to fill. The LLM never sees the article
table, only the facts dict built from this storyboard.

Articles arrive already annotated with `theme_group` (taxonomy.annotate),
which is what every theme chart counts on. `meta.taxonomy` carries the
group -> raw-label mapping so each row is auditable.

Issue selection (deterministic, documented in meta.issue.method):
  score(group) = growth_component + 3 x negative_share + 2 x unmet_needs_share
  growth_component = clamp((late - early) / max(early, 1), -1, 3)
  over groups with volume >= max(3, 2% of total), excluding Others.
Highest score is the emerging issue. The LLM names it from the facts.
"""

import math
from collections import Counter

from .. import brand_media, cohorts, quotes, taxonomy, timeseries

LENS_KEY = "track_emerging_issues"

TABS = [
    {"id": "t1", "label": "Issue Journey"},
    {"id": "t2", "label": "Behaviour & Usage"},
    {"id": "t3", "label": "Themes & Drivers"},
]
MAX_THEME_ROWS = 8
MAX_SPLIT_ROWS = 6
MAX_COHORT_ROWS = 5
DEFAULT_STAGE_COUNT = 4   # replaced by len(stages) once the LLM writes them


def _unmet(article: dict) -> bool:
    v = article.get("unmet needs") or article.get("unmet_needs")
    return bool(v) and str(v).strip().lower() not in {"none", "null", "n/a", "na", "-", ""}


def _floor(total: int) -> int:
    return max(3, math.ceil(total * 0.02))


def select_issue(articles: list[dict], *, brand: str = "", known: list[str] | None = None) -> dict | None:
    """The theme group behaving most like an emerging issue, with its evidence.

    score = growth + 3*negative_share + 2*unmet_needs_share + brand_share
      growth        = clamp((late - early) / max(early, floor), -1, 3), and 0
                      unless the group appears in at least two time buckets
                      (a one-week batch is not an emerging trend)
      brand_share   = share of the group's posts that name the subject brand
    over groups with volume >= floor = max(3, 2% of posts), Others excluded.
    """
    known = known or []
    counts = taxonomy.group_counts(articles)
    total = sum(counts.values())
    floor = _floor(total)
    grain = timeseries.granularity(articles)
    early_all, late_all = timeseries.halves(articles)
    early_c = taxonomy.group_counts(early_all)
    late_c = taxonomy.group_counts(late_all)

    best = None
    for name, n in counts.items():
        if name == taxonomy.OTHERS or n < floor:
            continue
        rows = [a for a in articles if a.get(taxonomy.GROUP_FIELD) == name]
        early, late = early_c.get(name, 0), late_c.get(name, 0)
        buckets_hit = timeseries.spread(rows, grain)
        growth = max(-1.0, min(3.0, (late - early) / max(early, floor))) if (late_all and buckets_hit >= 2) else 0.0
        neg = cohorts.negative_share(rows)
        unmet = sum(1 for a in rows if _unmet(a)) / len(rows)
        brand_share = len(cohorts.brand_articles(rows, brand, known)) / len(rows) if brand else 0.0
        score = growth + 3 * neg + 2 * unmet + brand_share
        cand = {
            "group": name, "mentions": n, "early": early, "late": late, "buckets": buckets_hit,
            "growth_pct": timeseries.growth_pct(early, late), "negative_share": round(neg * 100),
            "unmet_share": round(unmet * 100), "brand_share": round(brand_share * 100), "score": round(score, 3),
        }
        if best is None or (cand["score"], cand["mentions"]) > (best["score"], best["mentions"]):
            best = cand
    if best:
        best["method"] = (
            "growth + 3*negative_share + 2*unmet_needs_share + brand_share over theme groups "
            f"(volume floor {floor}, growth needs >= 2 {grain}s, Others excluded)"
        )
    return best


def _theme_rows(articles: list[dict], limit: int) -> list[dict]:
    counts = taxonomy.group_counts(articles)
    if not counts:
        return []
    ranked = sorted(counts, key=lambda k: (k == taxonomy.OTHERS, -counts[k], k))
    keep = ranked[:limit]
    folded = dict(counts)
    if len(ranked) > limit:
        folded = {k: counts[k] for k in keep if k != taxonomy.OTHERS}
        folded[taxonomy.OTHERS] = sum(counts[k] for k in ranked if k not in folded)
        keep = [k for k in keep if k != taxonomy.OTHERS] + [taxonomy.OTHERS]
    rows = cohorts.pct_rows(folded, order=keep)
    return [{"name": r["name"], "pct": r["pct"], "count": r["count"], "text": ""} for r in rows if r["pct"] > 0 or r["count"] > 0]


def _cohort_block(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    theme_rows = [{"name": r["name"], "pct": r["pct"]} for r in _theme_rows(rows, MAX_COHORT_ROWS)]
    quote = quotes.one(rows, prefer="Negative")
    return {"rows": theme_rows, "points": [], "quote": quote, "count": len(rows)}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    known = known_brands or ([brand] if brand else [])
    total = len(articles)
    platforms = cohorts.platforms(articles)
    window = timeseries.window_label(articles)
    issue = select_issue(articles, brand=brand, known=known)
    issue_rows = [a for a in articles if issue and a.get(taxonomy.GROUP_FIELD) == issue["group"]]
    brand_rows = cohorts.brand_articles(articles, brand, known)

    # ── tab 1: journey + trend ────────────────────────────────────────────
    # Issue mentions bucketed over the whole capture window (zeros where the
    # issue is silent), so the curve always spans the same axis as the data.
    all_keys, _, grain = timeseries.buckets(articles)
    points, grain = timeseries.count_series(issue_rows or articles, grain, keys=all_keys)
    pk = timeseries.peak(points)
    grain_word = "weekly" if grain == "week" else "monthly"
    anchors = timeseries.annotations_at(points)
    annotations = [
        {"at": a["at"], "label": f"{points[a['at']]['x']} · {a['kind']}", "text": "", "kind": a["kind"], "value": points[a["at"]]["y"]}
        for a in anchors
    ]
    journey = {
        "banner": {
            "eyebrow": "Issue Journey",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(DEFAULT_STAGE_COUNT), "label": "Lifecycle stages"},
                {"value": cohorts.fmt_int(pk["y"]) if pk else "—", "label": f"Peak {grain_word} mentions"},
                {"value": pk["x"] if pk else "—", "label": f"Peak {grain}"},
                {"value": str(len(platforms)), "label": "Platforms"},
            ],
        },
        "stages": [],
    }
    trend = {
        "title": "",
        "headline": "",
        "unit": "No. of mentions",
        "grain": grain,
        "points": [{"x": p["x"], "y": p["y"]} for p in points],
        "annotations": annotations,
        "footnote": f"Mentions are from: {', '.join(platforms)}." if platforms else "",
    }

    # ── tab 2: behaviour & usage ──────────────────────────────────────────
    issue_platforms = cohorts.pct_rows(dict(Counter(cohorts.platform(a) for a in (issue_rows or articles))))
    top_platform = issue_platforms[0] if issue_platforms else None
    brand_in_issue = len(cohorts.brand_articles(issue_rows, brand, known)) if issue_rows else 0
    behaviour = {
        "banner": {
            "eyebrow": "Behaviour & Usage",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": cohorts.fmt_int(len(issue_rows)), "label": "Issue mentions"},
                {"value": f"{round(brand_in_issue * 100 / len(issue_rows))}%" if issue_rows else "—", "label": f"Name {brand}" if brand else "Name the brand"},
                {"value": f"{top_platform['name']} · {top_platform['pct']}%" if top_platform else "—", "label": "Lead platform"},
            ],
        },
        "profile": [
            {"title": "Behaviour", "sub": "", "points": []},
            {"title": "Interests", "sub": "", "points": []},
            {"title": "Attitude", "sub": "", "points": []},
        ],
        "usage_note": "",
        "usage": [],
        "platform_split": issue_platforms[:6],
    }

    # ── tab 3: themes, motivation, single vs multiple ─────────────────────
    theme_rows = _theme_rows(articles, MAX_THEME_ROWS)
    top_group = theme_rows[0] if theme_rows else None
    net = cohorts.net_sentiment(articles)
    brand_share = round(len(brand_rows) * 100 / total) if total else 0
    issue_in_brand = len([a for a in brand_rows if issue and a.get(taxonomy.GROUP_FIELD) == issue["group"]])
    funnel = []
    if total and brand and brand_rows:
        funnel = [
            {"value": f"~{brand_share}%", "label": f"of all posts name {brand}"},
            {"value": f"~{round(issue_in_brand * 100 / len(brand_rows))}%", "label": f"of those relate to {issue['group'] if issue else 'the issue'}"},
        ]
    elif total and top_group and issue:
        in_top = len([a for a in articles if a.get(taxonomy.GROUP_FIELD) == top_group["name"]])
        funnel = [
            {"value": f"~{top_group['pct']}%", "label": f"of all posts are about {top_group['name']}"},
            {"value": f"~{round(issue['mentions'] * 100 / max(in_top, 1))}%" if issue["group"] != top_group["name"] else "100%", "label": f"of those relate to {issue['group']}"},
        ]
    themes = {
        "banner": {
            "eyebrow": "Themes & Drivers",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(len([r for r in theme_rows if r["name"] != taxonomy.OTHERS])), "label": "Themes of discussion"},
                {"value": f"{top_group['pct']}%" if top_group else "—", "label": f"Top theme · {top_group['name']}" if top_group else "Top theme"},
                {"value": ("+" if (net or 0) >= 0 else "") + (f"{net:g}" if net is not None else "—"), "label": "Net sentiment"},
            ],
        },
        "funnel": funnel,
        "rows": theme_rows,
    }

    driver_source = brand_rows if len(brand_rows) >= _floor(total) else articles
    split_rows = _theme_rows(driver_source, MAX_SPLIT_ROWS)
    motivation = {
        "note": "",
        "split": [{"name": r["name"], "pct": r["pct"]} for r in split_rows],
        "drivers": [{"title": r["name"], "text": ""} for r in split_rows if r["name"] != taxonomy.OTHERS],
        "basis": "brand posts" if driver_source is brand_rows else "all posts",
    }

    single, multiple = cohorts.single_multiple(articles, known)
    holders = cohorts.pct_rows({"Single brand": len(single), "Multiple brands": len(multiple)}, order=["Single brand", "Multiple brands"]) if (single or multiple) else []
    multi = {
        "note": "",
        "holders": [{"name": h["name"], "pct": h["pct"], "count": h["count"]} for h in holders] if len(holders) == 2 else [],
        "sentiment": [{"name": s["name"], "pct": s["pct"], "tone": s["tone"]} for s in cohorts.sentiment_split(articles)],
    }
    if multi["holders"]:
        s_block, m_block = _cohort_block(single), _cohort_block(multiple)
        if s_block:
            multi["single"] = s_block
        if m_block:
            multi["multiple"] = m_block

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "platforms": platforms,
            "total_mentions": total,
            "logos": brand_media.brand_logos([brand, *known], articles),
            "issue": issue,
            "grain": grain,
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Track Emerging Issues · Issues Intelligence", f"Computed from {total:,} tagged posts"],
        "journey": journey,
        "trend": trend,
        "behaviour": behaviour,
        "themes": themes,
        "motivation": motivation,
        "multi": multi,
        "evidence": {
            # Real excerpts the narrative can quote or paraphrase; never shown raw.
            "issue_quotes": quotes.pick(issue_rows or articles, needles=[brand] if brand else None, limit=4, prefer="Negative"),
            "brand_quotes": quotes.pick(brand_rows, needles=[brand] if brand else None, limit=3),
            "unmet_needs": sorted({str(a.get("unmet needs")).strip() for a in articles if _unmet(a)})[:8],
            "product_mentions": [n for n, _ in Counter(str(a.get("product mentions")).strip() for a in articles if a.get("product mentions") and str(a.get("product mentions")).strip().lower() != "none").most_common(8)],
        },
    }
