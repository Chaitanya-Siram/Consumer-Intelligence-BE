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
    "track_emerging_issues",
    "shifting_audience_priorities",
    "perception_analysis",
    "dominant_narratives",
    "brand_perception",
    "audience_expectation",
    "brand_messaging",
    "brand_performance",
    "user_behaviour",
    "regional_sentiment",
    "regional_engagement",
    "regional_brand_perception",
    "congruence_content",
    "social_research",
    "social_listening",
    "social_audit",
    "pr_research",
}

# Tier 1 keys whose backend is not yet implemented — return {"status": "coming_soon"}
COMING_SOON_TIER1: set[str] = {
    "influencer_mapping",
    "crisis_solutioning",
}

# Tier 1 key → list of CI lens keys to build.
# brand_intelligence also auto-includes brand_health_storyboard + brand_competitive_intel
# (handled in builder.py, matching ConsumerIntelligence_PR/backend/app/charts/builder.py).
TIER1_TO_LENS_KEYS: dict[str, list[str]] = {
    "brand_intelligence": ["brand_intelligence", "brand_perception"],
    "market_intelligence": ["market_intelligence"],
    "network_map_analysis": ["network_map"],
    # Tier 2 lenses shipped so far under these pillars. The other Tier 2s
    # ("Categorized", "Real-time", "Emerging Themes", ...) stay coming-soon in
    # the FE until a builder exists; add its LENS_KEY here when it does.
    "issues_intelligence": ["track_emerging_issues"],
    "advanced_metrics": ["shifting_audience_priorities"],
    "landscape_analysis": ["perception_analysis", "dominant_narratives"],
    "whitespace_gap_analysis": ["audience_expectation", "brand_messaging", "brand_performance"],
    # New Tier 1 pillar (FE: "Consumer Segmentation Analysis"); this is the only
    # place the backend enumerates Tier 1 keys, so adding it here is the whole change.
    "consumer_segmentation": ["user_behaviour"],
    "regional_intelligence": ["regional_sentiment", "regional_engagement", "regional_brand_perception"],
    # AI/LLM Audit and Analysis (FE label); the lens runs an LLM audit, not a tag aggregation.
    "llm_audit": ["congruence_content"],
    # Social Research: one lens, its Tier 2 sub-lenses (Brand Perception & Relevance,
    # Competitive & Cultural Landscape, Occasions & Social Behaviors, Social
    # Motivations & Identity, Cultural Spaces, Appendix) are tabs within it, not
    # separate lens keys — see social_research.py's TABS.
    "social_research": ["social_research"],
    # Social Listening: single-brand, no competitor set. Same "one lens, tabs
    # are sub-lenses" shape as Social Research.
    "social_listening": ["social_listening"],
    # Social Audit: single-brand, no competitor set. Three fixed research
    # pillars (Devices, AI, Screentime) are tabs within the one lens key.
    "social_audit": ["social_audit"],
    # PR Research: editorial/news lens built around an early-vs-late period
    # comparison (timeseries.halves), not a competitor set. Authors,
    # Publications, Audience Profile are tabs within the one lens key.
    "pr_research": ["pr_research"],
    # trend_intelligence is standalone (not a tier1 key in FE constants)
    # but included here for completeness
    "trend_intelligence": ["trend_intelligence"],
}

# Lens keys that are only built when the workflow node's `data.tier2[]` names
# the Tier 2 label. Lenses not listed here follow the Brand Intelligence
# behaviour: every registered lens under a selected Tier 1 is built.
TIER2_GATE: dict[str, str] = {
    "perception_analysis": "Perception Analysis",
    "dominant_narratives": "Dominant Narratives",
    "brand_perception": "Brand Perception",
    "audience_expectation": "Audience Expectation",
    "brand_messaging": "Brand Messaging",
    "brand_performance": "Brand Performance",
    "user_behaviour": "User Behaviour Analysis",
    "regional_sentiment": "State-Level Sentiment",
    "regional_engagement": "Engagement",
    "regional_brand_perception": "Brand Perception",
    "congruence_content": "Congruence & Content Intelligence",
}


def _tier2_allows(lens_key: str, node_data: dict) -> bool:
    label = TIER2_GATE.get(lens_key)
    if not label:
        return True
    selected = node_data.get("tier2") or []
    if not isinstance(selected, list):
        return False
    return any(str(s).strip().lower() == label.lower() for s in selected)


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
                keys = [k for k in TIER1_TO_LENS_KEYS.get(lens, []) if _tier2_allows(k, data)]
                lens_keys.update(keys)
        elif lens in CI_LENS_KEYS:
            lens_keys.add(lens)

    return list(lens_keys), list(coming_soon)


def is_ci_lens(lens: str) -> bool:
    return lens in CI_LENS_KEYS
