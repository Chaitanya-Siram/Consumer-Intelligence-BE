"""Tier 1 / Tier 2 lens registry for Consumer Intelligence.

Mirrors ConsumerIntelligence_PR/src/workflow/constants.js on the backend:
- TIER1_TO_LENS_KEYS: maps a Tier 1 key to the CI LENS_KEY(s) it builds
- COMING_SOON_TIER1: Tier 1 keys that have no live backend yet
- CI_LENS_KEYS: all CI lens keys the builder understands
"""

CI_LENS_KEYS: set[str] = {
    "trend_intelligence",
    "brand_intelligence",
    "brand_health_storyboard",
    "brand_competitive_intel",
    "market_intelligence",
    "network_map",
}

# Tier 1 keys whose backend is not yet implemented — return {"status": "coming_soon"}
COMING_SOON_TIER1: set[str] = {
    "landscape_analysis",
    "advanced_metrics",
    "influencer_mapping",
    "whitespace_gap_analysis",
    "regional_intelligence",
    "issues_intelligence",
    "crisis_solutioning",
}

# Tier 1 key → list of CI lens keys to build.
# brand_intelligence also auto-includes brand_health_storyboard + brand_competitive_intel
# (handled in builder.py, matching ConsumerIntelligence_PR/backend/app/charts/builder.py).
TIER1_TO_LENS_KEYS: dict[str, list[str]] = {
    "brand_intelligence": ["brand_intelligence"],
    "market_intelligence": ["market_intelligence"],
    "network_map_analysis": ["network_map"],
    # trend_intelligence is standalone (not a tier1 key in FE constants)
    # but included here for completeness
    "trend_intelligence": ["trend_intelligence"],
}


def resolve_ci_lenses(workflow_nodes: list[dict]) -> tuple[list[str], list[str]]:
    """Extract CI lens keys from workflow analysis nodes.

    Handles both:
    - lensType=='tier1' nodes → expand Tier 1 to CI lens keys
    - lensType=='intelligence' nodes where lens is a CI key (e.g. trend_intelligence standalone)

    Returns de-duplicated list of CI lens keys to build.
    """
    lens_keys: set[str] = set()
    coming_soon: set[str] = set()

    for node in workflow_nodes:
        if node.get("type") != "analysis":
            continue
        data = node.get("data", {})
        lens = str(data.get("lens") or "").strip()
        lens_type = str(data.get("lensType") or "intelligence").strip()

        if not lens:
            continue

        if lens_type == "tier1":
            if lens in COMING_SOON_TIER1:
                coming_soon.add(lens)
            else:
                keys = TIER1_TO_LENS_KEYS.get(lens, [])
                lens_keys.update(keys)
        elif lens in CI_LENS_KEYS:
            lens_keys.add(lens)

    return list(lens_keys), list(coming_soon)


def is_ci_lens(lens: str) -> bool:
    return lens in CI_LENS_KEYS
