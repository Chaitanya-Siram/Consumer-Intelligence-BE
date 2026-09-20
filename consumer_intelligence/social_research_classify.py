"""Per-article classification for the Social Research lens (Social Research
Tier 1: Brand Perception & Relevance, Occasions & Social Behaviors, Social
Motivations & Identity, Cultural Spaces).

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-research.md

One batched, temperature-0 LLM pass labels every post on five small fixed
vocabularies the generic tagger schema has no field for:

  occasion         drinking_occasions | hosting_entertaining | celebrations_social | none
  cultural_space   music | food_dining | sports_fashion | nightlife | community | none
  motivation       impression | belonging | hosting | identity | status | none
  entity           free-text specific named venue/event/platform the post names, or empty
  feel             aspirational | inclusive | premium | good_vibes | sustainable | none

Behavioral themes and their volume trend reuse the shared `taxonomy` module
(the same LLM-canonicalised theme-group system emerging_issues/audience_priorities
already use) rather than a sixth field here — a post's `theme_group` is already
on it by the time build_storyboard runs.

Fallbacks are keyword rules so the lens still builds (with `none` for a post
the rules can't place) when the LLM is unavailable.
"""

import asyncio
import logging
import re

from . import aggregate
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PREPARE_KEY = "social_research"

OCCASION_KEYS = ("drinking_occasions", "hosting_entertaining", "celebrations_social")
OCCASION_TITLES = {
    "drinking_occasions": "Drinking Occasions",
    "hosting_entertaining": "Hosting & Entertaining",
    "celebrations_social": "Celebrations & Social Habits",
}
OCCASION_DEFS = {
    "drinking_occasions": "When and where people drink socially — weeknights, weekends, watching sport, unwinding after work.",
    "hosting_entertaining": "Hosting guests, stocking up for a cookout or house party, being a good host.",
    "celebrations_social": "Milestone celebrations, group outings, treating yourself among friends.",
}

CULTURAL_SPACE_KEYS = ("music", "food_dining", "sports_fashion", "nightlife", "community")
CULTURAL_SPACE_TITLES = {
    "music": "Music",
    "food_dining": "Food & Dining",
    "sports_fashion": "Sports & Fashion",
    "nightlife": "Nightlife & Entertainment",
    "community": "Community & Creator Culture",
}
CULTURAL_SPACE_DEFS = {
    "music": "Concerts, festivals, artists, playlists, music culture.",
    "food_dining": "Restaurants, cooking, pairing with food, dining occasions.",
    "sports_fashion": "Sport viewing/attendance, fashion, style, apparel.",
    "nightlife": "Bars, clubs, nightlife, late-night socialising.",
    "community": "Local community events, creator communities, fan groups.",
}

MOTIVATION_KEYS = ("impression", "belonging", "hosting", "identity", "status")
MOTIVATION_TITLES = {
    "impression": "Making a Good Impression",
    "belonging": "Belonging & Friendship",
    "hosting": "Hosting & Entertaining",
    "identity": "Identity & Self-Expression",
    "status": "Social Status & Credibility",
}
MOTIVATION_DEFS = {
    "impression": "Choosing the brand to look good in front of others, a host, or new acquaintances.",
    "belonging": "Shared drinking/eating moments that reinforce friendship or in-group identity.",
    "hosting": "Picking a safe, crowd-pleasing choice so guests aren't disappointed.",
    "identity": "The brand as a marker of personal style, taste, or self-expression.",
    "status": "Premium/import framing lending credibility or status in a group setting.",
}

FEEL_KEYS = ("aspirational", "inclusive", "premium", "good_vibes", "sustainable")
FEEL_TITLES = {
    "aspirational": "Aspirational",
    "inclusive": "Inclusive",
    "premium": "Premium Quality",
    "good_vibes": "Good Vibes",
    "sustainable": "Sustainability",
}

BATCH = 60
CONCURRENCY = 3

_OCC_RULES = (
    ("hosting_entertaining", re.compile(r"host|cookout|bbq|barbecue|house party|guests?", re.I)),
    ("celebrations_social", re.compile(r"celebrat|birthday|anniversary|milestone|toast", re.I)),
    ("drinking_occasions", re.compile(r"weekend|game ?day|match ?day|unwind|after work|watch(ing)? the game", re.I)),
)
_SPACE_RULES = (
    ("music", re.compile(r"festival|concert|dj|playlist|artist|music", re.I)),
    ("food_dining", re.compile(r"restaurant|dinner|recipe|pairing|cook|dining|food", re.I)),
    ("sports_fashion", re.compile(r"match|stadium|league|f1\b|uefa|fashion|style|apparel|jersey", re.I)),
    ("nightlife", re.compile(r"bar|club|pub|nightlife|late[- ]night", re.I)),
    ("community", re.compile(r"community|fan club|creator|local event", re.I)),
)
_MOTIVE_RULES = (
    ("hosting", re.compile(r"host|guests?|cookout|party", re.I)),
    ("belonging", re.compile(r"friends|crew|squad|together|group", re.I)),
    ("identity", re.compile(r"my style|who i am|identity|express", re.I)),
    ("status", re.compile(r"premium|status|impress|credibility|prestige", re.I)),
    ("impression", re.compile(r"impress|look good|first date|new friends", re.I)),
)
_FEEL_RULES = (
    ("premium", re.compile(r"premium|quality|crisp|consistent", re.I)),
    ("aspirational", re.compile(r"aspir|global|elite|iconic", re.I)),
    ("inclusive", re.compile(r"everyone|inclusive|welcom", re.I)),
    ("sustainable", re.compile(r"sustain|eco|environment", re.I)),
    ("good_vibes", re.compile(r"vibe|fun|good times|great time", re.I)),
)


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def _brief(a: dict, chars: int = 260) -> dict:
    return {
        "id": article_id(a),
        "title": str(a.get("title") or "")[:140],
        "summary": str(a.get("summary") or a.get("content") or "")[:chars],
        "theme": str(a.get("theme") or "")[:60],
        "sentiment": a.get("sentiment"),
    }


def _rule_pick(rules: tuple, text: str, keys: tuple) -> str:
    for key, rx in rules:
        if rx.search(text):
            return key
    return "none"


def fallback_label(a: dict) -> dict:
    text = f"{a.get('theme') or ''} {a.get('title') or ''} {a.get('summary') or ''}"
    return {
        "occasion": _rule_pick(_OCC_RULES, text, OCCASION_KEYS),
        "cultural_space": _rule_pick(_SPACE_RULES, text, CULTURAL_SPACE_KEYS),
        "motivation": _rule_pick(_MOTIVE_RULES, text, MOTIVATION_KEYS),
        "entity": "",  # named-entity extraction has no reliable regex fallback
        "feel": _rule_pick(_FEEL_RULES, text, FEEL_KEYS),
    }


async def _label_batch(briefs: list[dict], brand: str, sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "occasions": OCCASION_DEFS,
        "cultural_spaces": CULTURAL_SPACE_DEFS,
        "motivations": MOTIVATION_DEFS,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "occasion": "|".join(OCCASION_KEYS) + "|none",
            "cultural_space": "|".join(CULTURAL_SPACE_KEYS) + "|none",
            "motivation": "|".join(MOTIVATION_KEYS) + "|none",
            "entity": "2-5 word proper-noun name of a SPECIFIC venue, event, platform or activation the post names the brand alongside "
                      "(e.g. 'Heineken Riverdeck', 'UEFA Champions League Final', 'Coachella'), or empty when the post names none",
            "feel": "|".join(FEEL_KEYS) + "|none — the emotional association the post expresses toward the brand, if any",
        }]},
    }
    system = (
        "You label consumer social posts about a brand for a social-research dashboard. "
        "Label each post on every field; use 'none'/empty freely when a field doesn't apply — most posts will be "
        "'none' on several fields. `entity` is a SPECIFIC named place/event/platform actually named in the text "
        "(a real proper noun), never a generic category like 'bars' or 'festivals' — leave it empty rather than "
        "generalising. Judge from the text only. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=180,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        occ = str(it.get("occasion") or "none").lower()
        space = str(it.get("cultural_space") or "none").lower()
        mot = str(it.get("motivation") or "none").lower()
        feel = str(it.get("feel") or "none").lower()
        entity = re.sub(r"\s+", " ", str(it.get("entity") or "")).strip(" .")[:60]
        out[str(it["id"])] = {
            "occasion": occ if occ in OCCASION_KEYS else "none",
            "cultural_space": space if space in CULTURAL_SPACE_KEYS else "none",
            "motivation": mot if mot in MOTIVATION_KEYS else "none",
            "entity": entity if entity.lower() not in ("none", "n/a") else "",
            "feel": feel if feel in FEEL_KEYS else "none",
        }
    return out


async def label_articles(articles: list[dict]) -> dict:
    briefs = [_brief(a) | {"id": article_id(a)} for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    brand = ""
    results = await asyncio.gather(*(_label_batch(b, brand, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Social research label batch failed: %s", r)
            continue
        by_id.update(r)
    by_article = {article_id(a): a for a in articles}
    for b in briefs:
        by_id.setdefault(b["id"], fallback_label(by_article[b["id"]]))
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels for every post. Never raises."""
    labels = await label_articles(articles)
    return {"labels": labels}


def label_of(a: dict, prepared: dict) -> dict:
    return (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {
        "occasion": "none", "cultural_space": "none", "motivation": "none", "entity": "", "feel": "none",
    }
