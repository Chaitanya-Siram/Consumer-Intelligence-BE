"""Consumer Intelligence orchestrator.

Mirrors ConsumerIntelligence_PR/backend/app/charts/builder.py:
  for each selected lens → build_storyboard() (CPU, off-thread)
                         → write_narrative() ‖ resolve hero media (concurrent)
Emits progress via `on_event` so the WS router can stream it.

Tier 1 `brand_intelligence` auto-bundles `brand_health_storyboard` and
`brand_competitive_intel`, matching the reference.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from . import brand_media
from .storyboard import bci, brand_intel, health, market_intel, network_map, trend
from .storyboard.narrative import write_narrative
from .tier_registry import COMING_SOON_TIER1, resolve_ci_lenses

logger = logging.getLogger(__name__)

EmitFn = Callable[[dict[str, Any]], Awaitable[None] | None]

_MODULES = {
    trend.LENS_KEY: trend,
    brand_intel.LENS_KEY: brand_intel,
    health.LENS_KEY: health,
    bci.LENS_KEY: bci,
    market_intel.LENS_KEY: market_intel,
    network_map.LENS_KEY: network_map,
}

# Build order: cheapest first so the client sees something quickly.
_ORDER = [
    trend.LENS_KEY,
    bci.LENS_KEY,
    health.LENS_KEY,
    brand_intel.LENS_KEY,
    network_map.LENS_KEY,
    market_intel.LENS_KEY,
]

_BUNDLE = {brand_intel.LENS_KEY: {health.LENS_KEY, bci.LENS_KEY}}

_HERO_QUERY = {
    trend.LENS_KEY: "consumer trends city crowd",
    brand_intel.LENS_KEY: "retail brand shopping",
    health.LENS_KEY: "customer satisfaction",
    bci.LENS_KEY: "business competition strategy",
    market_intel.LENS_KEY: "global market analysis",
    network_map.LENS_KEY: "network connections abstract",
}



_SENTIMENT_LABELS = {
    "pos": "Positive", "positive": "Positive",
    "neg": "Negative", "negative": "Negative",
    "neu": "Neutral", "neutral": "Neutral",
}


def normalize_articles(articles: list[dict]) -> list[dict]:
    """Shallow-copy articles with the platform's POS/NEG/NEU sentiment codes
    mapped to the Positive/Negative/Neutral labels the storyboards compare on."""
    out = []
    for article in articles:
        copy = dict(article)
        raw = str(copy.get("sentiment") or "").strip().lower()
        copy["sentiment"] = _SENTIMENT_LABELS.get(raw) or (copy.get("sentiment") if raw else None)
        out.append(copy)
    return out


def expand_lenses(lens_keys: list[str]) -> list[str]:
    selected = set(lens_keys)
    for parent, children in _BUNDLE.items():
        if parent in selected:
            selected |= children
    return [k for k in _ORDER if k in selected]


async def _emit(on_event: EmitFn | None, payload: dict[str, Any]) -> None:
    if on_event is None:
        return
    result = on_event(payload)
    if asyncio.iscoroutine(result):
        await result


async def _build_one(
    lens_key: str,
    articles: list[dict],
    brand: str,
    known_brands: list[str],
    on_event: EmitFn | None,
    *,
    with_media: bool,
) -> dict:
    module = _MODULES[lens_key]
    started = time.time()

    await _emit(on_event, {"type": "progress", "stage": "storyboard", "lens": lens_key, "message": f"Computing {lens_key} metrics…"})
    storyboard = await asyncio.to_thread(module.build_storyboard, articles, brand=brand, known_brands=known_brands)

    await _emit(on_event, {"type": "progress", "stage": "narrative", "lens": lens_key, "message": f"Writing {lens_key} narrative…"})
    tasks: list[Awaitable[Any]] = [write_narrative(lens_key, storyboard)]
    if with_media:
        tasks.append(brand_media.resolve_hero_media(f"{brand} {_HERO_QUERY[lens_key]}".strip()))
        tasks.append(brand_media.resolve_brand_assets(brand, articles))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    if with_media:
        hero_media = results[1] if not isinstance(results[1], Exception) else None
        brand_assets = results[2] if not isinstance(results[2], Exception) else None
        hero = storyboard.get("hero")
        if isinstance(hero, dict):
            if hero.get("media") is None and hero_media:
                hero["media"] = hero_media
            if brand_assets and not hero.get("logo_url"):
                hero["logo_url"] = brand_assets.get("logo_url")
        if brand_assets:
            storyboard.setdefault("meta", {})["brand_assets"] = brand_assets

    storyboard.setdefault("meta", {})["elapsed_seconds"] = round(time.time() - started, 1)
    logger.info("CI lens %s built in %.1fs", lens_key, time.time() - started)
    return storyboard


async def build_ci_charts(
    *,
    workflow_nodes: list[dict] | None,
    requested_lenses: list[str] | None,
    tagged_articles: list[dict],
    brand_keywords: list[str] | None,
    competitor_keywords: list[str] | None,
    on_event: EmitFn | None = None,
    with_media: bool = True,
) -> dict[str, Any]:
    """Build every selected CI lens. Returns `{lens_key: storyboard, ..., "coming_soon": {...}, "meta": {...}}`.

    Lens selection: `requested_lenses` if given, else resolved from the
    session's workflow analysis nodes. Unknown keys are ignored.
    """
    if requested_lenses:
        lens_keys = [k for k in requested_lenses if k in _MODULES]
        coming_soon: list[str] = [k for k in requested_lenses if k not in _MODULES and k in COMING_SOON_TIER1]
    else:
        lens_keys, coming_soon = resolve_ci_lenses(workflow_nodes or [])

    lens_keys = expand_lenses(lens_keys)
    tagged_articles = normalize_articles(tagged_articles)
    brand = (brand_keywords or [""])[0] or ""
    known = list(dict.fromkeys([b for b in [*(brand_keywords or []), *(competitor_keywords or [])] if b]))

    await _emit(on_event, {"type": "start", "lenses": lens_keys, "coming_soon": coming_soon, "total_articles": len(tagged_articles)})

    out: dict[str, Any] = {}
    started = time.time()
    for lens_key in lens_keys:
        try:
            out[lens_key] = await _build_one(lens_key, tagged_articles, brand, known, on_event, with_media=with_media)
            await _emit(on_event, {"type": "lens_complete", "lens": lens_key, "storyboard": out[lens_key]})
        except Exception as exc:
            logger.exception("CI lens %s failed", lens_key)
            out[lens_key] = {"meta": {"lens": lens_key, "error": str(exc)}, "status": "failed"}
            await _emit(on_event, {"type": "lens_error", "lens": lens_key, "detail": str(exc)})

    for key in coming_soon:
        out[key] = {"status": "coming_soon", "lens": key}

    out["coming_soon"] = {k: {"status": "coming_soon"} for k in coming_soon}
    out["meta"] = {
        "provider": "consumer_intelligence",
        "lenses": lens_keys,
        "coming_soon": coming_soon,
        "brand": brand,
        "competitors": [b for b in known if b != brand],
        "total_articles": len(tagged_articles),
        "elapsed_seconds": round(time.time() - started, 1),
    }
    return out
