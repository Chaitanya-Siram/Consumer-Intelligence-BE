"""Regional Intelligence · State-Level Sentiment (`regional_sentiment`).

Contract §4.1. Numbers from regional_base.compute_regions; prose left empty
for narrative.py. Shares the prepare() result with the other two regional
lenses through PREPARE_KEY = "regional".
"""

from .. import timeseries
from ..regional_classify import PREPARE_KEY, prepare
from .regional_base import compute_regions, project

LENS_KEY = "regional_sentiment"
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
        "footer": ["State-Level Sentiment · Regional Intelligence", f"Computed from {len(articles):,} tagged posts"],
        "note": "",
        "regions": project(regions, ("insights", "sentiment", "themes")),
        "evidence": {r["key"]: r["_evidence"] for r in regions},
    }
