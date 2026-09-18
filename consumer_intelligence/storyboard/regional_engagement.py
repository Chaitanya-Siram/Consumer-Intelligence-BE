"""Regional Intelligence · Engagement (`regional_engagement`).

Contract §4.2. Product-type shares per half of the session window, per
market; the two lists use the same merged type names so the screen can
compute H1 → H2 shifts by name. Shares prepare() with the other regional
lenses through PREPARE_KEY = "regional".
"""

from .. import timeseries
from ..regional_classify import PREPARE_KEY, prepare
from .regional_base import compute_regions, project

LENS_KEY = "regional_engagement"
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
        "footer": ["Engagement · Regional Intelligence", f"Computed from {len(articles):,} tagged posts"],
        "note": "",
        "periods": computed["periods"],
        "regions": project(regions, ("insights", "types_h1", "types_h2", "topics")),
        "evidence": {r["key"]: r["_evidence"] for r in regions},
    }
