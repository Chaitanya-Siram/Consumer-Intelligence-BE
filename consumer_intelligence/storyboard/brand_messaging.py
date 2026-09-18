"""Brand Messaging storyboard — Whitespace & Gap Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-whitespace-gap.md §4.2
Screen:   Consumer-Intelligence-FE/src/screens/BrandMessagingScreen.jsx

The tagger has no brand-owned / earned flag, so "brand-initiative" posts are
the ones the shared whitespace classifier labelled as being about something a
tracked brand did (campaign, offer, partnership, launch, programme). Each
brand's initiative labels are merged into 3–6 message themes; pct = share of
that brand's initiative posts. No `tabs`: the screen makes one tab per brand.
"""

from .. import brand_media, cohorts, timeseries
from ..whitespace_classify import PREPARE_KEY, label_of, prepare

LENS_KEY = "brand_messaging"
__all__ = ["LENS_KEY", "PREPARE_KEY", "build_storyboard", "prepare"]

MIN_POSTS_PER_BRAND = 3
MAX_COMPETITORS = 4
THIN_BRAND_POSTS = 10     # below this many initiative posts...
THIN_BRAND_THEMES = 3     # ...show at most this many themes plus "Other messages"
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)
    inits_meta: dict = prepared.get("initiatives") or {}

    by_brand: dict[str, list[dict]] = {}
    for a in articles:
        lab = label_of(a, prepared)
        if lab["initiative_brand"]:
            by_brand.setdefault(lab["initiative_brand"], []).append(a)

    def card(name: str) -> dict | None:
        rows = by_brand.get(name, [])
        if len(rows) < MIN_POSTS_PER_BRAND:
            return None
        merged = inits_meta.get(name) or {}
        tmap, titles = merged.get("map") or {}, {t["key"]: t["title"] for t in merged.get("themes", [])}
        by_theme: dict[str, list[dict]] = {}
        for a in rows:
            lab = label_of(a, prepared)["initiative"]
            k = tmap.get(lab) if lab else None
            if k:
                by_theme.setdefault(k, []).append(a)
        if not by_theme:
            return None
        # A brand with few initiative posts cannot support six one-post themes:
        # keep the top three and fold the rest into "Other messages".
        if len(rows) < THIN_BRAND_POSTS and len(by_theme) > THIN_BRAND_THEMES:
            ranked = sorted(by_theme, key=lambda k: (-len(by_theme[k]), k))
            keep, tail = ranked[:THIN_BRAND_THEMES], ranked[THIN_BRAND_THEMES:]
            folded = {k: by_theme[k] for k in keep}
            folded["other"] = [a for k in tail for a in by_theme[k]]
            titles = {**titles, "other": "Other messages"}
            by_theme = folded
        pct = cohorts.pct_rows({k: len(v) for k, v in by_theme.items()})
        return {
            "name": name,
            "is_brand": bool(brand) and name.lower() == brand.lower(),
            "mentions": len(rows),
            "headline": "",
            "sub": "",
            "initiatives": [{"key": r["name"], "title": titles.get(r["name"], r["name"].replace("-", " ").title()), "pct": r["pct"], "count": r["count"], "points": []} for r in pct[:6]],
            "_evidence": {r["name"]: [_brief(a) for a in by_theme[r["name"]][:SAMPLE]] for r in pct[:6]},
        }

    brands = []
    if brand:
        c = card(brand)
        if c:
            brands.append(c)
    competitors = sorted((n for n in by_brand if n.lower() != (brand or "").lower()), key=lambda n: (-len(by_brand[n]), n))
    for n in competitors:
        if len(brands) >= 1 + MAX_COMPETITORS:
            break
        c = card(n)
        if c:
            brands.append(c)
    evidence = {b["name"]: b.pop("_evidence") for b in brands}
    initiative_posts = sum(b["mentions"] for b in brands)

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "initiative_posts": initiative_posts,
            "logos": brand_media.brand_logos([brand, *[b["name"] for b in brands]], articles),
            "classification": {
                "labels": (prepared.get("labels") or {}).get("method"),
                "initiative_posts_by_brand": {k: len(v) for k, v in sorted(by_brand.items(), key=lambda kv: -len(kv[1]))},
                "themes": {b["name"]: [{"key": t["key"], "title": t["title"], "raw": t["raw"][:8]} for t in (inits_meta.get(b["name"]) or {}).get("themes", [])] for b in brands},
                "basis": "posts the classifier judged to be about something the brand did (no brand-owned/earned flag in the tagger)",
            },
        },
        "footer": ["Brand Messaging · Whitespace & Gap Analysis", f"Computed from {initiative_posts:,} brand-initiative posts"],
        "note": "",
        "brands": brands,
        "evidence": evidence,
    }
