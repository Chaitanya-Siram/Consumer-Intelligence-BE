"""Classification over an LLM audit run for the Congruence & Content lens.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-congruence-content.md §5.3-5.4.

Two temperature-0 passes over the audit responses (not the tagged articles):

1. `propose_pillars` — when the project carries no positioning statement,
   3-5 narrative pillars are proposed from the brand's own tagged posts and
   stored with the run (pillars_source = "proposed_from_tagged_articles").
   A user-confirmed list can replace them later without code changes.

2. `label_responses` — per response: sentiment toward the brand, up to three
   theme labels, the descriptors applied to the brand, and for every pillar
   whether the response reproduces / ignores / contradicts it. Theme labels
   merge to <= 8 via merge_labels so the matrix has stable rows.

Fallbacks keep the lens building: neutral sentiment, no themes, "ignore" for
every pillar, with the method recorded in meta.classification.
"""

import asyncio
import logging
import re

from .narrative_client import get_narrative_client
from .whitespace_classify import merge_labels

logger = logging.getLogger(__name__)

BATCH = 12
CONCURRENCY = 3
MAX_THEMES = 8
MAX_DESCRIPTORS = 10
STANCES = ("reproduce", "ignore", "contradict")


def _clean(v, n=48) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip(" .").lower()[:n]


async def propose_pillars(brand: str, category: str, brand_posts: list[dict], *, n_min: int = 3, n_max: int = 5) -> list[dict]:
    """[{"name","intent"}] proposed from the brand's own coverage. Empty list when it cannot be done."""
    briefs = [{"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:220], "sentiment": a.get("sentiment")} for a in brand_posts[:40]]
    if not briefs:
        return []
    payload = {"brand": brand, "category": category, "posts": briefs, "schema": {"pillars": [{"name": "str 2-5 words, the claim the brand appears to want repeated", "intent": "str one sentence stating the claim as the brand would"}]}}
    system = (
        f"You infer a brand's intended narrative pillars: the {n_min}-{n_max} distinct claims about {brand} that its own coverage, product "
        "descriptions and promotions keep repeating (e.g. protection, ease of use, value, heritage). Use only what the posts support; "
        "no generic marketing claims the posts do not make. Return ONLY JSON."
    )
    try:
        raw = await asyncio.wait_for(get_narrative_client().complete_json([{"role": "system", "content": system}, {"role": "user", "content": f"INPUT:\n{payload}"}], temperature=0.0), timeout=90)
        out = []
        for it in (raw.get("pillars") if isinstance(raw, dict) else None) or []:
            if isinstance(it, dict) and str(it.get("name") or "").strip():
                out.append({"name": str(it["name"]).strip()[:48], "intent": str(it.get("intent") or "").strip()[:200]})
        return out[:n_max] if len(out) >= n_min else out[:n_max]
    except Exception as exc:
        logger.warning("Pillar proposal failed: %s", exc)
        return []


async def _label_batch(batch: list[dict], brand: str, pillars: list[dict], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand,
        "pillars": [{"key": p["key"], "name": p["name"], "intent": p["intent"]} for p in pillars],
        "responses": [{"id": r["id"], "assistant": r["llm"], "prompt": r["prompt"][:160], "text": r["text"][:1800]} for r in batch],
        "schema": {"responses": [{
            "id": "id from input",
            "sentiment": "pos|neg|neu — the response's overall stance toward the brand",
            "themes": ["1-3 labels, 2-4 words each, naming what the response says the brand is about (e.g. 'interior protection', 'value pricing', 'ease of use')"],
            "descriptors": ["0-5 adjectives or short phrases the response applies to the brand, as written"],
            "pillars": [{"key": "pillar key", "stance": "reproduce|ignore|contradict"}],
        }]},
    }
    system = (
        "You audit AI assistant answers about a brand. For each response give its sentiment toward the brand, the themes it "
        "conveys, the descriptors it uses, and for each narrative pillar whether the response reproduces the claim, ignores it, "
        "or asserts the opposite. Judge from the text only. Return ONLY JSON."
    )
    ids = {r["id"] for r in batch}
    async with sem:
        raw = await asyncio.wait_for(get_narrative_client().complete_json([{"role": "system", "content": system}, {"role": "user", "content": f"Label these responses.\n\nINPUT:\n{payload}"}], temperature=0.0), timeout=180)
    out: dict[str, dict] = {}
    pkeys = {p["key"] for p in pillars}
    for it in (raw.get("responses") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        s = str(it.get("sentiment") or "neu").lower()
        stances = {}
        for p in it.get("pillars") or []:
            if isinstance(p, dict) and p.get("key") in pkeys:
                st = str(p.get("stance") or "ignore").lower()
                stances[p["key"]] = st if st in STANCES else "ignore"
        out[str(it["id"])] = {
            "sentiment": s if s in ("pos", "neg", "neu") else "neu",
            "themes": [t for t in (_clean(x) for x in (it.get("themes") or [])[:3]) if t],
            "descriptors": [t for t in (_clean(x, 32) for x in (it.get("descriptors") or [])[:5]) if t],
            "pillars": {k: stances.get(k, "ignore") for k in pkeys},
        }
    return out


async def label_responses(responses: list[dict], *, brand: str, pillars: list[dict]) -> dict:
    """{"by_id": {id: {...}}, "themes": merge result, "method"}"""
    if not responses:
        return {"by_id": {}, "themes": {"themes": [], "map": {}, "method": "empty"}, "method": "empty"}
    batches = [responses[i : i + BATCH] for i in range(0, len(responses), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, brand, pillars, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Audit label batch failed: %s", r)
            continue
        by_id.update(r)
    for r in responses:
        by_id.setdefault(r["id"], {"sentiment": "neu", "themes": [], "descriptors": [], "pillars": {p["key"]: "ignore" for p in pillars}})
    counts: dict[str, int] = {}
    for lab in by_id.values():
        for t in lab["themes"]:
            counts[t] = counts.get(t, 0) + 1
    themes = await merge_labels(counts, min_n=3, max_n=MAX_THEMES, kind="themes AI assistants convey about the brand", brand=brand)
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "themes": themes, "method": method}
