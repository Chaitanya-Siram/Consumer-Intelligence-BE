"""Regional Intelligence · Brand Perception (`regional_brand_perception`).

Contract §4.3. Brand shares per market reuse brands.mention_counts (the
brand_competitive_intel tally) over brand-tagged posts in that market, top
5-8 plus Others, folded names listed in `others`. Shares prepare() with the
other regional lenses through PREPARE_KEY = "regional".
"""

from .. import timeseries
from ..regional_classify import PREPARE_KEY, prepare
from .regional_base import compute_regions, project

LENS_KEY = "regional_brand_perception"
__all__ = ["LENS_KEY", "PREPARE_KEY", "build_storyboard", "prepare"]


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    computed = compute_regions(articles, brand=brand, known=known, prepared=prepared or {})
    regions = computed["regions"]
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": timeseries.window_label(articles),
            "total_mentions": len(articles),
            "logos": computed["logos"],
            "classification": computed["meta_extra"],
        },
        "footer": ["Brand Perception · Regional Intelligence", f"Computed from {len(articles):,} tagged posts"],
        "note": "",
        "regions": project(regions, ("brands", "others", "topics")),
        "evidence": {r["key"]: r["_evidence"] for r in regions},
    }
