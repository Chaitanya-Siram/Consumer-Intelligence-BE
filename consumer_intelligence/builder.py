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

from . import brand_media, taxonomy
from .storyboard import (
    audience_priorities,
    bci,
    brand_intel,
    brand_perception,
    dominant_narratives,
    emerging_issues,
    health,
    market_intel,
    network_map,
    perception,
    trend,
)
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
    emerging_issues.LENS_KEY: emerging_issues,
    audience_priorities.LENS_KEY: audience_priorities,
    perception.LENS_KEY: perception,
    dominant_narratives.LENS_KEY: dominant_narratives,
    brand_perception.LENS_KEY: brand_perception,
}

# Lenses that count on LLM-canonicalised theme groups (taxonomy.annotate).
# The taxonomy is computed once per build and shared across these lenses.
_NEEDS_TAXONOMY = {emerging_issues.LENS_KEY, audience_priorities.LENS_KEY}

# Lenses whose module exposes `async prepare(articles, brand=, known_brands=)`
# get its result passed to build_storyboard(prepared=...). Used for per-lens
# LLM classification (e.g. perception themes, negative emotion, narrative labels).
_HAS_PREPARE = {perception.LENS_KEY, dominant_narratives.LENS_KEY, brand_perception.LENS_KEY}

# Build order: cheapest first so the client sees something quickly.
_ORDER = [
    trend.LENS_KEY,
    bci.LENS_KEY,
    health.LENS_KEY,
    brand_intel.LENS_KEY,
    network_map.LENS_KEY,
    market_intel.LENS_KEY,
    emerging_issues.LENS_KEY,
    audience_priorities.LENS_KEY,
    perception.LENS_KEY,
    dominant_narratives.LENS_KEY,
    brand_perception.LENS_KEY,
]

_BUNDLE = {brand_intel.LENS_KEY: {health.LENS_KEY, bci.LENS_KEY}}

_HERO_QUERY = {
    trend.LENS_KEY: "consumer trends city crowd",
    brand_intel.LENS_KEY: "retail brand shopping",
    health.LENS_KEY: "customer satisfaction",
    bci.LENS_KEY: "business competition strategy",
    market_intel.LENS_KEY: "global market analysis",
    network_map.LENS_KEY: "network connections abstract",
    emerging_issues.LENS_KEY: "customer complaint attention warning",
    audience_priorities.LENS_KEY: "loyal customers audience priorities",
    perception.LENS_KEY: "consumer perception emotions",
    dominant_narratives.LENS_KEY: "conversation landscape narratives",
    brand_perception.LENS_KEY: "brand perception shoppers",
}

# Lenses whose screens carry their own hero art; skip Pexels for them.
_NO_HERO_MEDIA = {emerging_issues.LENS_KEY, audience_priorities.LENS_KEY, perception.LENS_KEY, dominant_narratives.LENS_KEY, brand_perception.LENS_KEY}



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
    theme_taxonomy: dict | None = None,
) -> dict:
    module = _MODULES[lens_key]
    started = time.time()

    await _emit(on_event, {"type": "progress", "stage": "storyboard", "lens": lens_key, "message": f"Computing {lens_key} metrics…"})
    kwargs: dict[str, Any] = {"brand": brand, "known_brands": known_brands}
    if lens_key == audience_priorities.LENS_KEY:
        kwargs["taxonomy_groups"] = theme_taxonomy or {}
    if lens_key in _HAS_PREPARE:
        await _emit(on_event, {"type": "progress", "stage": "classify", "lens": lens_key, "message": f"Classifying posts for {lens_key}…"})
        try:
            kwargs["prepared"] = await module.prepare(articles, brand=brand, known_brands=known_brands)
        except Exception as exc:  # classifiers have their own fallbacks; this is belt and braces
            logger.warning("CI prepare failed for %s: %s", lens_key, exc)
            kwargs["prepared"] = {}
    storyboard = await asyncio.to_thread(module.build_storyboard, articles, **kwargs)
    if lens_key in _NEEDS_TAXONOMY and theme_taxonomy is not None:
        storyboard.setdefault("meta", {})["taxonomy"] = taxonomy.summary(theme_taxonomy)

    await _emit(on_event, {"type": "progress", "stage": "narrative", "lens": lens_key, "message": f"Writing {lens_key} narrative…"})
    tasks: list[Awaitable[Any]] = [write_narrative(lens_key, storyboard)]
    if lens_key in _NO_HERO_MEDIA:
        with_media = False
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
    skip_lenses: set[str] | None = None,
) -> dict[str, Any]:
    """Build every selected CI lens. Returns `{lens_key: storyboard, ..., "coming_soon": {...}, "meta": {...}}`.

    Lens selection: `requested_lenses` if given, else resolved from the
    session's workflow analysis nodes. Unknown keys are ignored.
    `skip_lenses` are selected lenses the caller already has (cached) and does
    not want rebuilt; they are reported in meta but not built.
    """
    if requested_lenses:
        lens_keys = [k for k in requested_lenses if k in _MODULES]
        coming_soon: list[str] = [k for k in requested_lenses if k not in _MODULES and k in COMING_SOON_TIER1]
    else:
        lens_keys, coming_soon = resolve_ci_lenses(workflow_nodes or [])

    lens_keys = expand_lenses(lens_keys)
    to_build = [k for k in lens_keys if k not in (skip_lenses or set())]
    tagged_articles = normalize_articles(tagged_articles)
    brand = (brand_keywords or [""])[0] or ""
    known = list(dict.fromkeys([b for b in [*(brand_keywords or []), *(competitor_keywords or [])] if b]))

    await _emit(on_event, {"type": "start", "lenses": to_build, "cached": sorted(set(lens_keys) - set(to_build)), "coming_soon": coming_soon, "total_articles": len(tagged_articles)})

    theme_taxonomy: dict | None = None
    if any(k in _NEEDS_TAXONOMY for k in to_build):
        await _emit(on_event, {"type": "progress", "stage": "taxonomy", "lens": None, "message": "Grouping themes…"})
        theme_taxonomy = await taxonomy.canonicalize(tagged_articles, brand=brand)
        taxonomy.annotate(tagged_articles, theme_taxonomy)
        logger.info("CI taxonomy: %s groups via %s from %s raw themes", len(theme_taxonomy.get("groups", [])), theme_taxonomy.get("method"), theme_taxonomy.get("raw_distinct"))

    out: dict[str, Any] = {}
    started = time.time()
    for lens_key in to_build:
        try:
            out[lens_key] = await _build_one(lens_key, tagged_articles, brand, known, on_event, with_media=with_media, theme_taxonomy=theme_taxonomy)
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
        "built": to_build,
        "coming_soon": coming_soon,
        "brand": brand,
        "competitors": [b for b in known if b != brand],
        "total_articles": len(tagged_articles),
        "elapsed_seconds": round(time.time() - started, 1),
    }
    return out
