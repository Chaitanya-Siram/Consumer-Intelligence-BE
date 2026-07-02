"""Link related articles after tagging.

Adds two pointer fields to each tagged article, both referencing the
EARLIEST-published article in the group (the canonical "main" article):

  - syndication_of: same title, different source — a republished copy. Detected
                    lexically (normalized title equality / prefix / high ratio).
                    Deterministic, no LLM.
  - similar_of:     same underlying story / meaning, different wording. Detected
                    by an LLM clustering pass over the non-copy "representatives".

Run this AFTER reorder_by_confidence so the pointers use the final A{n} ids.
"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from configs import logger
from agents.chart_generator.llm_client import complete_json


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _normalize(title: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace — a comparable title key."""
    t = str(title or "").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _date_key(article: dict) -> tuple[str, str]:
    """Sort key making the earliest-published article first. ISO dates sort
    lexically; a missing date sorts last so it never wrongly becomes the main."""
    return (str(article.get("date") or "9999"), str(article.get("id") or ""))


def _earliest(members: list[dict]) -> dict:
    return sorted(members, key=_date_key)[0]


# ---------------------------------------------------------------------------
# Syndication — lexical (same title, different source)
# ---------------------------------------------------------------------------

def _syndication_match(a: str, b: str) -> bool:
    """True when two normalized titles are effectively the same headline. Handles
    aggregator suffixes (one title is a prefix of the other) and minor edits."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # e.g. "… asco 2026" is a prefix of "… asco 2026 business timesargus com".
    if longer.startswith(shorter) and len(shorter) >= 0.6 * len(longer):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.95


def _link_syndication(items: list[dict]) -> set[str]:
    """Cluster near-identical titles (union-find), point copies at the earliest
    article in each cluster. Returns the set of ids that were marked as copies."""
    ids = [a["id"] for a in items]
    norms = {a["id"]: _normalize(a.get("title")) for a in items}
    by_id = {a["id"]: a for a in items}

    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _syndication_match(norms[ids[i]], norms[ids[j]]):
                union(ids[i], ids[j])

    clusters: dict[str, list[str]] = defaultdict(list)
    for i in ids:
        clusters[find(i)].append(i)

    copies: set[str] = set()
    for member_ids in clusters.values():
        if len(member_ids) < 2:
            continue
        members = [by_id[i] for i in member_ids]
        primary = _earliest(members)
        for m in members:
            if m["id"] != primary["id"]:
                m["syndication_of"] = primary["id"]
                copies.add(m["id"])
    return copies


# ---------------------------------------------------------------------------
# Similar — semantic (same story, different wording), via the LLM
# ---------------------------------------------------------------------------

_SIMILAR_SYSTEM = (
    "You group news articles that report the SAME underlying story or event — the "
    "same facts, just worded differently. Group ONLY genuine duplicates in meaning, "
    "NOT articles that merely share a topic or brand. Most articles will not belong "
    "to any group. Respond with JSON only."
)

_SIMILAR_INSTRUCTION = (
    'Return JSON of the form {"clusters": [["A1","A4"], ...]} where each inner list '
    "contains 2 or more article ids that tell the same story. Omit any article that "
    "has no same-story match. Use only the ids shown above."
)


def _link_similar(reps: list[dict], main_ids: set[str] | None = None) -> None:
    """Ask the LLM to cluster same-meaning articles among the representatives, then
    point each cluster's later articles at the primary one. Best-effort: any
    failure leaves similar_of blank.

    `main_ids` are the ids of syndication mains (articles that already own
    syndicated copies). A main must stay the primary of its similar cluster —
    otherwise it would become a `similar_of` child of another article while still
    owning its syndicated copies. So a main is preferred as primary over the plain
    earliest-by-date pick."""
    main_ids = main_ids or set()
    candidates = [a for a in reps if (a.get("title") or a.get("content"))]
    if len(candidates) < 2:
        return
    by_id = {a["id"]: a for a in candidates}

    lines = []
    for a in candidates:
        title = str(a.get("title") or "").strip()
        snippet = str(a.get("content") or "").strip()[:200]
        lines.append(f"{a['id']}: {title} :: {snippet}")
    user = "Articles:\n" + "\n".join(lines) + "\n\n" + _SIMILAR_INSTRUCTION

    data = complete_json(_SIMILAR_SYSTEM, user, max_tokens=2048)
    clusters = data.get("clusters") if isinstance(data, dict) else None
    if not isinstance(clusters, list):
        logger.warning("Similar clustering returned no usable 'clusters'; skipping")
        return

    for cluster in clusters:
        member_ids = [i for i in (cluster or []) if i in by_id]
        if len(member_ids) < 2:
            continue
        members = [by_id[i] for i in member_ids]
        # Prefer a syndication main as the primary; among several, the earliest;
        # otherwise the earliest overall.
        mains = [m for m in members if m["id"] in main_ids]
        primary = _earliest(mains if mains else members)
        for m in members:
            if m["id"] != primary["id"]:
                m["similar_of"] = primary["id"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def link_articles(articles: list[dict]) -> list[dict]:
    """Annotate articles in place with syndication_of / similar_of and return them."""
    items = [a for a in articles if isinstance(a, dict) and a.get("id")]
    for a in items:
        a.setdefault("syndication_of", "")
        a.setdefault("similar_of", "")

    syndicates = _link_syndication(items)

    # Ids of syndication mains (articles that own copies) — they must remain the
    # primary of any similar cluster they fall into.
    main_ids = {a["syndication_of"] for a in items if a.get("syndication_of")}

    # Cluster only the main articles.
    reps = [a for a in items if a["id"] not in syndicates]
    try:
        _link_similar(reps, main_ids)
    except Exception:
        logger.exception("Similar-article clustering failed; leaving similar_of blank")

    return articles
