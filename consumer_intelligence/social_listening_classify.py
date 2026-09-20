"""Per-article classification for the Social Listening lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-listening.md

Unlike Social Research, this lens has no competitor set — it tracks how a
brand's own recurring conversation pillars (top `taxonomy` theme groups,
generic and dataset-driven rather than a hardcoded phrase list — the source
deck's four named "expressions" are this session's specific instance of
"top theme groups", not a fixed vocabulary this module should hardcode) get
used, where, and in what tone. One batched, temperature-0 LLM pass labels
every post on four small fixed vocabularies the generic tagger schema has
no field for:

  conversation_setting  everyday | political | sports | entertainment | gaming | finance | none
  occasion_relevant     bool — the post ties the pillar to a specific occasion
  occasion_type         milestone | holiday | planned_party | solo_celebration |
                         spontaneous_gathering | hosting | none (only set when occasion_relevant)
  literal_vs_figurative literal | figurative | none — whether the tracked term/theme
                         is used in its literal sense or as slang/expression

Fallbacks are keyword rules so the lens still builds (with `none`/False for a
post the rules can't place) when the LLM is unavailable.
"""

import asyncio
import logging
import re

from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

PREPARE_KEY = "social_listening"

CONVERSATION_SETTING_KEYS = ("everyday", "political", "sports", "entertainment", "gaming", "finance")
CONVERSATION_SETTING_TITLES = {
    "everyday": "Everyday & Personal",
    "political": "Political & Social Commentary",
    "sports": "Sports",
    "entertainment": "Entertainment & Pop Culture",
    "gaming": "Gaming",
    "finance": "Crypto & Finance",
}
CONVERSATION_SETTING_DEFS = {
    "everyday": "Ordinary personal life, no particular news hook.",
    "political": "Politics, elections, public figures, social commentary.",
    "sports": "Sports teams, athletes, games, sporting events.",
    "entertainment": "Movies, TV, music, celebrities, pop culture, memes.",
    "gaming": "Video games, gaming culture, streamers.",
    "finance": "Crypto, stocks, money, investing.",
}

OCCASION_TYPE_KEYS = ("milestone", "holiday", "planned_party", "solo_celebration", "spontaneous_gathering", "hosting")
OCCASION_TYPE_TITLES = {
    "milestone": "Milestone / Life Event",
    "holiday": "Holiday",
    "planned_party": "Planned Party",
    "solo_celebration": "Solo Celebration",
    "spontaneous_gathering": "Spontaneous Gathering",
    "hosting": "Hosting & Entertaining",
}

BATCH = 60
CONCURRENCY = 3

_SETTING_RULES = (
    ("political", re.compile(r"election|senator|president|politic|congress|policy", re.I)),
    ("sports", re.compile(r"\bnba\b|\bnfl\b|\bmlb\b|game ?day|match|championship|finals", re.I)),
    ("entertainment", re.compile(r"movie|celebrity|award|oscars|grammy|show|music festival", re.I)),
    ("gaming", re.compile(r"\bgame\b|gaming|streamer|twitch|esports", re.I)),
    ("finance", re.compile(r"crypto|bitcoin|stock|invest|nft", re.I)),
)
_OCCASION_RULES = (
    ("milestone", re.compile(r"wedding|graduation|anniversary|promotion|new job|new house", re.I)),
    ("holiday", re.compile(r"holiday|christmas|thanksgiving|new ?year|fourth of july|independence day", re.I)),
    ("planned_party", re.compile(r"party|celebration planned|throwing a", re.I)),
    ("solo_celebration", re.compile(r"treat myself|solo|by myself|just me", re.I)),
    ("spontaneous_gathering", re.compile(r"last minute|spontaneous|impromptu|out of nowhere", re.I)),
    ("hosting", re.compile(r"hosting|guests|cookout|house party", re.I)),
)
_FIGURATIVE_RX = re.compile(r"let'?s cook|let him cook|let them cook|no notes|pop off|\bslay\b", re.I)


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


def _rule_pick(rules: tuple, text: str) -> str:
    for key, rx in rules:
        if rx.search(text):
            return key
    return "none"


def fallback_label(a: dict) -> dict:
    text = f"{a.get('theme') or ''} {a.get('title') or ''} {a.get('summary') or ''}"
    occ = _rule_pick(_OCCASION_RULES, text)
    return {
        "conversation_setting": _rule_pick(_SETTING_RULES, text),
        "occasion_relevant": occ != "none",
        "occasion_type": occ,
        "literal_vs_figurative": "figurative" if _FIGURATIVE_RX.search(text) else "none",
    }


async def _label_batch(briefs: list[dict], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "conversation_settings": CONVERSATION_SETTING_DEFS,
        "occasion_types": OCCASION_TYPE_TITLES,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "conversation_setting": "|".join(CONVERSATION_SETTING_KEYS) + "|none",
            "occasion_relevant": "bool — the post ties its topic to a specific occasion or celebration",
            "occasion_type": "|".join(OCCASION_TYPE_KEYS) + "|none — only when occasion_relevant is true",
            "literal_vs_figurative": "literal|figurative|none — is the post's key term/phrase used in its literal, "
                                      "dictionary sense, or as slang/a figurative expression? none if the post doesn't use such a term at all",
        }]},
    }
    system = (
        "You label consumer social posts for a social-listening dashboard. Label each post on every field; use "
        "'none'/false freely when a field doesn't apply — most posts will be 'none' on several fields. Judge from "
        "the text only. Return ONLY JSON."
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
        setting = str(it.get("conversation_setting") or "none").lower()
        occ_type = str(it.get("occasion_type") or "none").lower()
        lit = str(it.get("literal_vs_figurative") or "none").lower()
        out[str(it["id"])] = {
            "conversation_setting": setting if setting in CONVERSATION_SETTING_KEYS else "none",
            "occasion_relevant": bool(it.get("occasion_relevant")),
            "occasion_type": occ_type if occ_type in OCCASION_TYPE_KEYS else "none",
            "literal_vs_figurative": lit if lit in ("literal", "figurative") else "none",
        }
    return out


async def label_articles(articles: list[dict]) -> dict:
    briefs = [_brief(a) | {"id": article_id(a)} for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Social listening label batch failed: %s", r)
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
        "conversation_setting": "none", "occasion_relevant": False, "occasion_type": "none", "literal_vs_figurative": "none",
    }
