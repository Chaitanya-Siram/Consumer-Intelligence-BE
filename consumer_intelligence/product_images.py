"""Product photos for the Brand Perception product cards.

A dataset carries product *names* ("Armor All Extreme Tire Shine Spray"), never
product photos, and retailer pages block automated capture. So each product gets
a Pexels stock photo of its product type: the name with the brand stripped, plus
the storyboard's category. The FE labels it as a stock photo; it is not the
exact SKU. A product named only by its brand (nothing to picture) gets none.

Never raises; a product with no photo is left as it was.
"""

import asyncio
import logging
import re

from . import brand_media

logger = logging.getLogger(__name__)

_CONCURRENCY = 4
_MAX_QUERY_WORDS = 6


def product_query(name: str, brand: str, category: str = "") -> str | None:
    """Search text for a product photo, or None when the name is only the brand."""
    base = re.sub(re.escape(str(brand or "")), " ", str(name or ""), flags=re.I)
    base = re.sub(r"[^A-Za-z0-9 ]+", " ", base).strip()
    if not base:
        return None
    words = base.split()[:_MAX_QUERY_WORDS]
    return " ".join([*words, *str(category or "").split()[:2]])


async def attach(storyboard: dict) -> None:
    """Set `image` on every `perception.products[]` row a photo could be found for."""
    products = (storyboard.get("perception") or {}).get("products")
    if not isinstance(products, list):
        return
    category = (storyboard.get("meta") or {}).get("category") or ""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(row: dict) -> None:
        query = product_query(row.get("name"), row.get("brand"), category)
        if not query:
            return
        async with sem:
            photo = await brand_media.pexels_photo(query, orientation="landscape")
        if photo:
            row["image"] = photo

    try:
        await asyncio.gather(*(one(row) for row in products if isinstance(row, dict)))
    except Exception as exc:  # belt and braces — photos are decoration
        logger.warning("product image pass failed: %s", exc)
