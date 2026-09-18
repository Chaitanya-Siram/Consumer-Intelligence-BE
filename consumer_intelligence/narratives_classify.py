"""Per-article classification for the Dominant Narratives lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-dominant-narratives.md §5.

One batched, temperature-0 LLM pass labels every relevant post with:
  usage     — patterns | engagement | perception | none   (tab 1 groups)
  question  — q1 | q2 | q3 | q4 | none                    (tab 2 intents)
  outlook   — a 2-4 word attitude label, or ""            (tab 4 open coding)
A second small call merges the distinct outlook labels into 6-9 themes.

Fallbacks keep the lens honest when the LLM is unavailable: usage from
keyword rules on `theme`, question = none (columns then say less), outlook
from the shared taxonomy groups.

Every mapping is returned so the payload can carry it under
meta.classification for audit. The LLM never produces a number here.
"""

import asyncio
import logging
import re

from . import aggregate, taxonomy
from .narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

USAGE_KEYS = ("patterns", "engagement", "perception")
USAGE_TITLES = {"patterns": "Usage patterns", "engagement": "Usage engagement", "perception": "Card perception"}
USAGE_DEFS = {
    "patterns": "How the product is used: the jobs, situations, surfaces or occasions it is applied to; routines and how-tos.",
    "engagement": "How often or how much: purchase frequency, spend, bundles and kits, deals that trigger buying, repeat use.",
    "perception": "How the product or brand is regarded: quality, trust, reputation, comparisons, awards, criticism.",
}
QUESTION_KEYS = ("q1", "q2", "q3", "q4")
QUESTION_DEFS = {
    "q1": "Why the audience needs or buys the category at all (the underlying need).",
    "q2": "General perception and emotion about the category (how they feel about it).",
    "q3": "What motivates owning or trying more than one product or brand in the category.",
    "q4": "How they decide which product to use or buy from the options they have (decision criteria).",
}
MAX_OUTLOOK_THEMES = 9
MIN_OUTLOOK_THEMES = 6
BATCH = 60
CONCURRENCY = 3

_USAGE_RULES = (
    ("engagement", re.compile(r"promotion|discount|deal|sale|bundle|kit|offer|price|listing|purchase|buy|subscription|frequency", re.I)),
    ("perception", re.compile(r"review|critique|trust|award|quality|reputation|compar|rating|perform|feature|recommend|endorse|brand", re.I)),
    ("patterns", re.compile(r"usage|use|how[- ]?to|guide|routine|detailing|cleaning|application|apply|tip|maintenance|care", re.I)),
)


def keyword_usage(label: str) -> str:
    for key, rx in _USAGE_RULES:
        if rx.search(label or ""):
            return key
    return "none"


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


async def _label_batch(briefs: list[dict], brand: str, sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "usage_groups": USAGE_DEFS,
        "questions": QUESTION_DEFS,
        "posts": briefs,
        "schema": {"posts": [{"id": "id from input", "usage": "patterns|engagement|perception|none", "question": "q1|q2|q3|q4|none", "outlook": "2-4 word label for the money/consumer attitude the post reveals, or empty string"}]},
    }
    system = (
        "You label consumer posts about a brand and its category for a narratives dashboard. For each post pick "
        "exactly one usage group (or none), exactly one question it answers (or none), and a short outlook label "
        "naming the consumer attitude or priority it reveals (e.g. 'value for money', 'convenience first', "
        "'brand loyalty', 'DIY pride'); leave outlook empty when the post is pure news with no consumer attitude. "
        "Judge from the text only. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=150,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        usage = str(it.get("usage") or "none").lower()
        q = str(it.get("question") or "none").lower()
        outlook = re.sub(r"\s+", " ", str(it.get("outlook") or "")).strip(" .").lower()[:40]
        out[str(it["id"])] = {
            "usage": usage if usage in USAGE_KEYS else "none",
            "question": q if q in QUESTION_KEYS else "none",
            "outlook": outlook,
        }
    return out


async def label_articles(articles: list[dict], *, brand: str = "") -> dict:
    """{"by_id": {id: {usage, question, outlook}}, "method": "llm"|"fallback"}."""
    briefs = [_brief(a) for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, brand, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Narratives label batch failed: %s", r)
            continue
        by_id.update(r)
    if not by_id:
        return {"by_id": {}, "method": "fallback"}
    # Posts the LLM skipped in a batch fall back to keywords for usage only.
    for b in briefs:
        by_id.setdefault(b["id"], {"usage": keyword_usage(b["theme"]), "question": "none", "outlook": ""})
    return {"by_id": by_id, "method": "llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)"}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:40] or "theme"


async def merge_outlook(labels: dict[str, int], *, brand: str = "") -> dict:
    """Merge raw outlook labels (label -> post count) into 6-9 themes.

    Returns {"themes": [{"key","title","raw":[...]}], "map": {raw: key}, "method"}."""
    if not labels:
        return {"themes": [], "map": {}, "method": "empty"}
    ranked = sorted(labels, key=lambda k: (-labels[k], k))[:150]
    if len(ranked) <= MAX_OUTLOOK_THEMES:
        themes = [{"key": _slug(l), "title": l.capitalize(), "raw": [l]} for l in ranked]
        return {"themes": themes, "map": {l: _slug(l) for l in ranked}, "method": "direct"}
    payload = {
        "brand": brand or None,
        "min_themes": MIN_OUTLOOK_THEMES,
        "max_themes": MAX_OUTLOOK_THEMES,
        "labels": [{"label": l, "count": labels[l]} for l in ranked],
        "schema": {"themes": [{"title": "str 2-5 words, Title Case", "raw": ["labels from input"]}]},
    }
    system = (
        "You merge near-duplicate consumer-attitude labels into a small set of distinct outlook themes for a "
        "dashboard. Every input label appears in exactly one theme. Prefer merging singletons into a broader "
        "theme over creating tiny ones. Titles are short noun phrases. Return ONLY JSON."
    )
    try:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Merge these labels.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=90,
        )
        themes, assigned = [], set()
        for it in (raw.get("themes") if isinstance(raw, dict) else None) or []:
            if not isinstance(it, dict):
                continue
            title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()[:48]
            members = [str(m).strip().lower() for m in (it.get("raw") or []) if str(m).strip().lower() in labels and str(m).strip().lower() not in assigned]
            if title and members:
                themes.append({"key": _slug(title), "title": title, "raw": members})
                assigned.update(members)
        if not themes:
            raise ValueError("no usable themes")
        leftover = [l for l in ranked if l not in assigned]
        if leftover:
            themes.sort(key=lambda t: -sum(labels[r] for r in t["raw"]))
            themes[-1]["raw"].extend(leftover)
        themes.sort(key=lambda t: -sum(labels[r] for r in t["raw"]))
        if len(themes) > MAX_OUTLOOK_THEMES:
            head, tail = themes[: MAX_OUTLOOK_THEMES - 1], themes[MAX_OUTLOOK_THEMES - 1 :]
            head.append({"key": "other-attitudes", "title": "Other Attitudes", "raw": [r for t in tail for r in t["raw"]]})
            themes = head
        seen: set[str] = set()
        for t in themes:  # unique keys
            base, k, i = t["key"], t["key"], 2
            while k in seen:
                k, i = f"{base}-{i}", i + 1
            t["key"] = k
            seen.add(k)
        return {"themes": themes, "map": {r: t["key"] for t in themes for r in t["raw"]}, "method": "llm"}
    except Exception as exc:
        logger.warning("Outlook merge fell back to top labels: %s", exc)
        keep = ranked[: MAX_OUTLOOK_THEMES - 1]
        themes = [{"key": _slug(l), "title": l.capitalize(), "raw": [l]} for l in keep]
        rest = [l for l in ranked if l not in keep]
        if rest:
            themes.append({"key": "other-attitudes", "title": "Other Attitudes", "raw": rest})
        return {"themes": themes, "map": {r: t["key"] for t in themes for r in t["raw"]}, "method": "fallback"}


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Labels + merged outlook themes. Never raises."""
    labelled = await label_articles(articles, brand=brand)
    counts: dict[str, int] = {}
    for lab in labelled["by_id"].values():
        if lab.get("outlook"):
            counts[lab["outlook"]] = counts.get(lab["outlook"], 0) + 1
    if counts:
        outlook = await merge_outlook(counts, brand=brand)
    else:
        # No attitude labels (LLM down or pure-news data): reuse the theme taxonomy.
        tx = await taxonomy.canonicalize(articles, brand=brand)
        themes = [{"key": _slug(g["name"]), "title": g["name"], "raw": g["raw"]} for g in tx.get("groups", []) if g["name"] != taxonomy.OTHERS]
        outlook = {"themes": themes, "map": {r: t["key"] for t in themes for r in t["raw"]}, "method": f"taxonomy:{tx.get('method')}", "by_theme_field": True}
    return {"labels": labelled, "outlook": outlook}


def usage_of(a: dict, prepared: dict) -> str:
    lab = (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a))
    if lab:
        return lab.get("usage", "none")
    return keyword_usage(str(a.get("theme") or ""))


def question_of(a: dict, prepared: dict) -> str:
    lab = (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a))
    return lab.get("question", "none") if lab else "none"


def outlook_of(a: dict, prepared: dict) -> str | None:
    outlook = prepared.get("outlook") or {}
    mapping = outlook.get("map") or {}
    if outlook.get("by_theme_field"):
        label = str(a.get("theme") or "").strip()
        return mapping.get(label) if label and not aggregate.is_junk(label) else None
    lab = (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a))
    raw = (lab or {}).get("outlook") or ""
    return mapping.get(raw) if raw else None
