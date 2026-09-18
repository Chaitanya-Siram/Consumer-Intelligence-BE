"""LLM prose for every CI storyboard.

Follows ConsumerIntelligence_PR/backend/app/charts/storyboard/narrative.py:
  1. `_facts_<lens>(storyboard)` condenses numbers into a compact dict
  2. one JSON-mode chat call with a strict schema in the system prompt
  3. `_apply_<lens>(storyboard, raw)` merges field-by-field, defensively

`write_narrative()` never raises — on any failure the storyboard is returned
with prose fields still empty so the numbers always ship.
"""

import asyncio
import json
import logging
import re

from ..narrative_client import get_narrative_client

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a senior consumer-intelligence analyst writing storyboard copy for a "
    "brand dashboard built from tagged news coverage. Write crisp, specific, "
    "executive-grade prose. Every sentence must be grounded in the FACTS JSON — "
    "cite numbers from it, never invent figures, brands, or events. Avoid filler "
    "and marketing cliché. Return ONLY a JSON object matching the SCHEMA exactly; "
    "every key must be present; strings may be empty when nothing can be said."
)


def _s(value, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def _list(value, limit: int = 6, item_chars: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_s(v, item_chars) for v in value if _s(v)][:limit]


async def _ask(facts: dict, schema: dict, task: str) -> dict:
    client = get_narrative_client()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"TASK:\n{task}\n\nSCHEMA (return exactly these keys):\n"
                f"{json.dumps(schema, indent=1)}\n\nFACTS:\n{json.dumps(facts, ensure_ascii=False)}"
            ),
        },
    ]
    result = await client.complete_json(messages)
    return result if isinstance(result, dict) else {}


# ── trend_intelligence ────────────────────────────────────────────────────


def _facts_trend(sb: dict) -> dict:
    """The aggregates the model is allowed to write about (mirrors reference)."""
    mode = sb["meta"]["dataset_mode"]
    return {
        "meta": {k: v for k, v in sb["meta"].items() if k not in ("logos", "days")},
        "signals": [
            {
                "id": s["id"],
                "name": s["name"],
                "stage": s["stage"],
                "growth_pct": s["growth"],
                "volume": s["volume"],
                "net_sentiment": s["net_sentiment"],
                # Omitted for a brand export: it is ~100% for every signal there and
                # the model reads it as a competitive win rather than an artefact.
                **({"brand_capture_pct": s["brand_capture"]} if mode == "category" else {}),
                "leaders": s["leaders"][:3],
            }
            for s in sb["signals"]
        ],
        "share_of_voice": sb["sov"][:6],
        "net_sentiment_league": sb["net_sentiment"][:6],
        "capture_ranking": sb["capture_ranking"],
        "capture_label": sb["meta"].get("capture_label", ""),
        "kpi_cards": [
            {"label": k["label"], "value": k["value"], "delta": k["delta"]}
            for k in (sb["tabs"][0]["kpis"] if sb["tabs"] else [])
        ],
        "priorities": [
            {"id": p["signal_id"], "name": p["name"], "score": p["score"]}
            for p in sb["priorities"]
        ],
        "verdict_groups": [
            {"title": c["title"], "signals": [i["label"] for i in c["items"]]}
            for c in sb["verdict_columns"]
        ],
        "modal_keys": {
            key: {"title": m["title"], "stats": m["stats"]}
            for key, m in sb["modals"].items()
        },
        "tabs": [{"id": t["id"], "label": t["label"], "number": t["number"]} for t in sb["tabs"]],
        "quotes": [q["text"][:160] for q in sb["quotes"][:3]],
    }


_TAB_SHAPE = {
    "headline": "str <= 9 words",
    "sub": "str one sentence",
    "badges": ["3 very short stat labels"],
    "context": "str 2-3 sentences on why this matters",
    "kpi_backs": ["one explanation per KPI card, in order (tab1 only; [] otherwise)"],
    "whats_next": {
        "title": "str <= 10 words",
        "sub": "str one sentence",
        "actions": [{"title": "str <= 4 words", "body": "str one short line"}],
    },
}

_SCHEMA_TREND = {
    "hero": {"title": "str <= 12 words, the report's argument", "subtitle": "str one sentence", "chips": ["3 short context labels"]},
    "tabs": {"tab1": _TAB_SHAPE, "tab2": _TAB_SHAPE, "tab3": _TAB_SHAPE},
    "callout": "str - one sentence naming the sharpest contrast in the data",
    "priorities": [{"name": "signal name from facts.priorities", "why": "2 sentences: why it ranks here and what to do"}],
    "verdict": [{"column": "title from facts.verdict_groups", "label": "signal name", "text": "one clause on how to treat this signal"}],
    "modals": {"<key from facts.modal_keys>": ["2-3 short paragraphs: what the number is, what explains it, what to do"]},
    "footer": ["2-3 one-line methodology notes"],
}


def _apply_trend(sb: dict, raw: dict) -> None:
    hero = raw.get("hero") if isinstance(raw.get("hero"), dict) else {}
    sb["hero"]["title"] = _s(hero.get("title"), 120) or (sb["meta"]["brand"] or "Trend Intelligence")
    sb["hero"]["subtitle"] = _s(hero.get("subtitle"), 400)
    sb["hero"]["chips"] = _list(hero.get("chips"), 4, 60)

    tabs_prose = raw.get("tabs") if isinstance(raw.get("tabs"), dict) else {}
    for position, tab in enumerate(sb["tabs"]):
        prose = tabs_prose.get(tab["id"])
        if not isinstance(prose, dict):
            prose = {}
        tab["banner"]["headline"] = _s(prose.get("headline"), 140) or tab["label"]
        tab["banner"]["sub"] = _s(prose.get("sub"), 400)
        tab["banner"]["badges"] = _list(prose.get("badges"), 3, 60)
        tab["context"]["body"] = _s(prose.get("context"), 900)

        backs = _list(prose.get("kpi_backs"), len(tab["kpis"]), 400)
        for card, back in zip(tab["kpis"], backs):
            card["back"] = back

        nxt = prose.get("whats_next") if isinstance(prose.get("whats_next"), dict) else {}
        actions = nxt.get("actions") if isinstance(nxt.get("actions"), list) else []
        # Ordered from the NEXT tab onward and wrapping, so the CTA reads forward.
        others = sb["tabs"][position + 1:] + sb["tabs"][:position]
        arrow = "\u21ba" if tab["id"] == "tab3" else "\u2192"
        tab["whats_next"] = {
            "eyebrow": "The Verdict" if tab["id"] == "tab3" else "What's Next",
            "title": _s(nxt.get("title"), 120),
            "sub": _s(nxt.get("sub"), 300),
            "actions": [
                {
                    "num": f"{arrow} {target['number']}",
                    "title": (_s(a.get("title"), 60) if isinstance(a, dict) else "") or target["label"],
                    "body": _s(a.get("body"), 200) if isinstance(a, dict) else "",
                    "target": target["id"],
                }
                for a, target in zip(actions + [{}] * len(others), others)
            ],
            "cta": {
                "label": "RESTART" if tab["id"] == "tab3" else "CONTINUE",
                "name": others[0]["label"] if others else "",
                "target": others[0]["id"] if others else tab["id"],
            },
        }

    sb["callout"] = _s(raw.get("callout"))

    # priorities: match by name (never replace the array)
    why_by_name: dict[str, str] = {}
    items = raw.get("priorities")
    if isinstance(items, dict):
        why_by_name = {str(k): _s(v, 400) for k, v in items.items()}
    elif isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                key = _s(item.get("name") or item.get("signal") or item.get("id"), 120)
                if key:
                    why_by_name[key] = _s(item.get("why") or item.get("action"), 400)
    for priority in sb["priorities"]:
        priority["why"] = why_by_name.get(priority["name"]) or why_by_name.get(priority["signal_id"], "")

    # verdict columns: match by column title + item label (never replace the arrays)
    verdict = raw.get("verdict") if raw.get("verdict") is not None else raw.get("verdict_columns")
    by_label: dict[tuple[str, str], str] = {}
    if isinstance(verdict, dict):
        for label, text in verdict.items():
            by_label[("", str(label))] = _s(text, 300)
    elif isinstance(verdict, list):
        for item in verdict:
            if isinstance(item, dict):
                by_label[(_s(item.get("column"), 80), _s(item.get("label"), 120))] = _s(item.get("text"), 300)
    for column in sb["verdict_columns"]:
        for item in column["items"]:
            item["text"] = (
                by_label.get((column["title"], item["label"]))
                or by_label.get(("", item["label"]))
                or ""
            )

    # modals: match by key
    modals = raw.get("modals") if isinstance(raw.get("modals"), dict) else {}
    for key, modal in sb["modals"].items():
        paragraphs = modals.get(key)
        if isinstance(paragraphs, str):
            paragraphs = [paragraphs]
        modal["paragraphs"] = _list(paragraphs, 4, 700)

    sb["footer"] = _list(raw.get("footer"), 3, 200) or [
        f"{sb['meta']['total_conversations']:,} conversations \u00b7 {sb['meta']['window_label']}",
        "InfoVision / AlphaMetricx \u00b7 Consumer Intelligence",
    ]


# ── brand_competitive_intel ───────────────────────────────────────────────


def _facts_bci(sb: dict) -> dict:
    return {
        "meta": {k: v for k, v in sb["meta"].items() if k not in ("logos", "days")},
        # Without this the model wrote a brand-engagement headline onto the
        # competitor tab, because a brand export gives it little else to say there.
        "tab_subjects": {
            "t1": "overall volume, sentiment split and engagement",
            "t2": "what the brand's own conversation is about and what drives each sentiment",
            "t3": "how the brand compares with the competitors present in this dataset",
        },
        "kpis": [{"label": k["label"], "value": k["value"], "sub": k["sub"]} for k in sb["kpis"]],
        "sentiment_split": sb["sentiment_split"],
        "platforms": sb["platforms"][:6],
        "themes": sb["themes"][:6],
        "entities": sb["items"][:6],
        "positive_drivers": sb["positive_drivers"][:5],
        "negative_drivers": sb["negative_drivers"][:5],
        "operational_issues": sb["operational_issues"][:5],
        "platform_engagement": sb["platform_engagement"],
        "competitor_table": sb["competitor_ranks"][:6],
        "avg_engagement": sb["avg_engagement"][:6],
        # Excerpts only, so the model can name a story without quoting at length.
        "top_post_excerpts": [
            {"sentiment": p["sentiment"], "source": p["source"], "engagement": p["engagement"], "excerpt": p["text"][:160]}
            for p in sb["top_posts"][:5]
        ],
    }


_SCHEMA_BCI = {
    "tabs": {
        "t1": {"headline": "str <= 12 words, the report's argument - about tab_subjects.t1", "sub": "str 2 sentences of context"},
        "t2": {"headline": "str <= 12 words - about tab_subjects.t2", "sub": "str 2 sentences"},
        "t3": {"headline": "str <= 12 words - about tab_subjects.t3", "sub": "str 2 sentences"},
    },
    "kpi_tags": ["one short badge per KPI card, in order, <= 6 words; must ADD something the card does not show (a rank, a share, a comparison, a caution) - never restate the value"],
    "drivers": [{"title": "str <= 8 words naming the story", "text": "str 2-3 sentences grounded in themes and posts", "tag": "str <= 5 words", "tone": "pos|neg"}],
    "callout": "str - one sentence naming the single sharpest finding",
    "footer": ["2-3 one-line notes"],
}


def _apply_bci(sb: dict, raw: dict) -> None:
    tabs_prose = raw.get("tabs") if isinstance(raw.get("tabs"), dict) else {}
    if isinstance(raw.get("tabs"), list):  # tolerate a list of {id, headline, sub}
        tabs_prose = {t.get("id"): t for t in raw["tabs"] if isinstance(t, dict)}
    for tab in sb["tabs"]:
        prose = tabs_prose.get(tab["id"])
        if isinstance(prose, dict):
            tab["banner"]["headline"] = _s(prose.get("headline"), 140) or tab["label"]
            tab["banner"]["sub"] = _s(prose.get("sub") or prose.get("body"), 500)
        elif not tab["banner"].get("headline"):
            tab["banner"]["headline"] = tab["label"]

    tags = _list(raw.get("kpi_tags"), len(sb["kpis"]), 60)
    for card, tag in zip(sb["kpis"], tags):
        card["tag"] = tag

    drivers = raw.get("drivers") if isinstance(raw.get("drivers"), list) else []
    sb["drivers"] = [
        {
            "tone": d.get("tone") if d.get("tone") in ("pos", "neg") else "pos",
            "title": _s(d.get("title"), 100),
            "text": _s(d.get("text"), 600),
            "tag": _s(d.get("tag"), 60),
        }
        for d in drivers[:4]
        if isinstance(d, dict) and _s(d.get("title"))
    ]

    sb["callout"] = _s(raw.get("callout"))
    sb["footer"] = _list(raw.get("footer"), 3, 200) or [
        f"{sb['meta']['total_conversations']:,} tagged posts \u00b7 {sb['meta']['window_label']}",
        "InfoVision Intelligence \u00b7 Confidential",
    ]


# ── brand_health_storyboard ───────────────────────────────────────────────


def _facts_health(sb: dict) -> dict:
    return {
        "brand": sb["meta"]["brand"],
        "competitors": sb["meta"]["competitors"][:6],
        "window": sb["meta"]["window_label"],
        "total_articles": sb["meta"]["total_conversations"],
        "classified": sb["meta"]["classified"],
        "bhi": {k: v for k, v in sb["bhi"].items() if k != "contributions"},
        "dimensions": [
            {
                "name": d["name"],
                "score": d["score"],
                "band": d["band"],
                "volume": d["volume"],
                "net_sentiment": d["net_sentiment"],
                "top_sub_kpis": [k["name"] for k in d["sub_kpis"][:3]],
            }
            for d in sb["dimensions"]
        ],
        "competitive": {
            "rank": sb["competitive"]["rank"],
            "share_of_voice": sb["competitive"]["share_of_voice"][:5],
            "co_mentions": sb["competitive"]["co_mentions"][:4],
        },
    }


_SCHEMA_HEALTH = {
    "hero": {"title": "str ≤ 12 words", "subtitle": "str 1-2 sentences", "chips": ["3-4 short stat chips"]},
    "dimensions": [{"name": "dimension name", "headline": "str", "body": "str 2-3 sentences", "insights": ["2-3 bullets"]}],
    "callout": "str — the composite verdict, 1-2 sentences",
    "footer": ["2-3 one-line notes incl. that dimensions are keyword-derived from news"],
}


def _apply_health(sb: dict, raw: dict) -> None:
    hero = raw.get("hero") or {}
    sb["hero"]["title"] = _s(hero.get("title"), 120)
    sb["hero"]["subtitle"] = _s(hero.get("subtitle"), 400)
    sb["hero"]["chips"] = _list(hero.get("chips"), 4, 60)
    by_name = {d["name"]: d for d in sb["dimensions"]}
    for item in raw.get("dimensions") or []:
        if isinstance(item, dict) and item.get("name") in by_name:
            d = by_name[item["name"]]
            d["headline"] = _s(item.get("headline"), 140)
            d["body"] = _s(item.get("body"))
            d["insights"] = _list(item.get("insights"), 3)
    sb["callout"] = _s(raw.get("callout"))
    sb["footer"] = _list(raw.get("footer"), 3, 200)


# ── brand_intelligence ────────────────────────────────────────────────────


def _facts_brand_intel(sb: dict) -> dict:
    return {
        "brand": sb["meta"]["brand"],
        "competitors": sb["meta"]["competitors"][:6],
        "window": sb["meta"]["window_label"],
        "kpis": sb["hero"]["kpis"],
        "trends": [
            {
                "id": t["id"],
                "label": t["label"],
                "stat": t["banner"]["stat"],
                "growth": t.get("growth"),
                "sentiment": t.get("sentiment"),
                "leaders": [{"name": l["name"], "mentions": l["mentions"], "is_subject": l["is_subject"]} for l in t["leaders"]["items"]],
                "verbatims": [v["text"][:160] for v in t.get("verbatims", [])[:2]],
            }
            for t in sb["tabs"]
            if "growth" in t
        ],
    }


_SCHEMA_BRAND_INTEL = {
    "hero_subtitle": "str 1-2 sentences",
    "overview_context": "str 2-3 sentences framing the category shifts",
    "cards": [{"tab_id": "id from facts", "desc": "str 1 sentence"}],
    "tabs": [
        {
            "id": "id from facts",
            "banner_desc": "str 1-2 sentences",
            "context": "str 2-3 sentences on why this trend matters",
            "leaders": [{"name": "brand name from facts", "desc": "str 1 sentence"}],
            "whats_next": {"title": "str", "sub": "str", "cta": "str ≤ 5 words"},
        }
    ],
    "strategy": {"body": "str 3-4 sentences", "formula": ["3-4 short phrases"]},
}


def _apply_brand_intel(sb: dict, raw: dict) -> None:
    sb["hero"]["subtitle"] = _s(raw.get("hero_subtitle"), 400)
    sb["overview"]["context"] = _s(raw.get("overview_context"))
    cards = {c["tab_id"]: c for c in sb["overview"]["cards"]}
    for item in raw.get("cards") or []:
        if isinstance(item, dict) and item.get("tab_id") in cards:
            cards[item["tab_id"]]["desc"] = _s(item.get("desc"), 240)
    tabs = {t["id"]: t for t in sb["tabs"]}
    for item in raw.get("tabs") or []:
        if not isinstance(item, dict) or item.get("id") not in tabs:
            continue
        t = tabs[item["id"]]
        if "banner" in t:
            t["banner"]["desc"] = _s(item.get("banner_desc"), 400)
        t["context"] = _s(item.get("context"))
        if "leaders" in t:
            by_name = {l["name"]: l for l in t["leaders"]["items"]}
            for l in item.get("leaders") or []:
                if isinstance(l, dict) and l.get("name") in by_name:
                    by_name[l["name"]]["desc"] = _s(l.get("desc"), 240)
        wn = item.get("whats_next") or {}
        if "whats_next" in t and isinstance(wn, dict):
            t["whats_next"].update(
                {"title": _s(wn.get("title"), 100), "sub": _s(wn.get("sub"), 200), "cta": _s(wn.get("cta"), 40)}
            )
    strat = raw.get("strategy") or {}
    for t in sb["tabs"]:
        if "strategy" in t:
            t["strategy"]["body"] = _s(strat.get("body"), 900)
            t["strategy"]["formula"] = _list(strat.get("formula"), 4, 80)


# ── market_intelligence ───────────────────────────────────────────────────


def _facts_market(sb: dict) -> dict:
    return {
        "brand": sb["meta"]["brand"],
        "competitors": sb["meta"]["competitors"][:6],
        "window": sb["meta"]["window_label"],
        "periods": {"a": sb["meta"]["period_a"], "b": sb["meta"]["period_b"]},
        "channel_impact": {
            "leaders": sb["channel_impact"]["leaders"],
            "sov": sb["channel_impact"]["sov"],
            "by_platform": sb["channel_impact"].get("by_platform", {}),
            "periods": [{"label": p["label"], "n": p["n"], "total": p["total"], "leaders": p["leaders"]} for p in sb["channel_impact"].get("periods", [])],
            "change": sb["channel_impact"].get("change", {}),
            "cards": [{"topic": c["topic"], "chip": c["chip"]} for c in sb["channel_impact"].get("takeaways", {}).get("cards", [])] if isinstance(sb["channel_impact"].get("takeaways"), dict) else [],
        },
        "industry_trends": sb["industry_trends"]["top"][:5],
        "trend_tracking": [r for s in sb["trend_tracking"]["slides"] for r in s["rows"]][:8],
        "volume": {k: sb["volume_trendline"].get(k) for k in ("total", "peak", "low", "growth_pct", "peak_subthemes")},
        "key_themes": sb["key_themes"]["insights"][:6],
        "top_brands": [{k: v for k, v in b.items() if k != "logo_url"} for b in sb["voice_of_user"]["top_brands"]],
        "regional_prefs": [{k: v for k, v in r.items() if k not in ("flag",)} for r in sb["voice_of_user"]["regional_prefs"][:6]],
        "brands": [
            {"name": b["name"], "mentions": b["mentions"], "pct": b["pct"], "positive_text": b.get("positive_text"), "lead_platform": b.get("lead_platform")}
            for b in sb["brand_analysis"]["brands"]
        ],
        "regions": [
            {"name": r["name"], "n": r["n"], "share": r["share"], "leader": r["leader"], "top_themes": r["top_themes"]}
            for r in sb["regional"]["regions"][:6]
        ],
    }


_SCHEMA_MARKET = {
    "channel_impact": {
        "headline": "str",
        "summary": "str one sentence on the loudest channel and who leads it",
        "takeaways": {
            "headline": "str one sentence: the volume/platform movement",
            "thesis": "str one sentence: who leads and why it matters",
            "cards": [{"topic": "topic from facts.channel_impact.cards", "title": "str <= 5 words", "text": "str 1-2 sentences"}],
        },
    },
    "industry_trends": {"headline": "str", "drivers": [{"name": "category from facts", "drivers": ["2 bullets"]}]},
    "trend_tracking": {"headline": "str", "insights": [{"category": "from facts", "insight": "str 1 sentence"}]},
    "volume_trendline": {"headline": "str", "drivers": ["2-3 bullets"]},
    "key_themes": {"headline": "str", "insights": [{"theme": "from facts", "text": "str 2 sentences"}]},
    "voice_of_user": {"headline": "str 2 short paragraphs", "brands": [{"name": "from facts", "bullets": ["2-3 bullets"]}], "regional": [{"region": "from facts", "driver": "str 1 sentence"}]},
    "brand_analysis": {"global_paragraphs": ["3-4 short paragraphs"], "takeaways": ["2 one-liners"], "brands": [{"name": "from facts", "summary": "str 2-3 sentences"}]},
    "regional": [{"name": "region from facts", "summary": "str 1-2 sentences", "key_insights": ["2-3 bullets"]}],
}


def _apply_market(sb: dict, raw: dict) -> None:
    ci = raw.get("channel_impact") if isinstance(raw.get("channel_impact"), dict) else {}
    block = sb["channel_impact"]
    block["headline"] = _s(ci.get("headline"))
    block["summary"] = _s(ci.get("summary"), 400) or block.get("summary", "")
    tk = ci.get("takeaways")
    existing = block.get("takeaways") if isinstance(block.get("takeaways"), dict) else {"headline": "", "thesis": "", "cards": []}
    if isinstance(tk, dict):
        existing["headline"] = _s(tk.get("headline"), 400)
        existing["thesis"] = _s(tk.get("thesis"), 400)
        by_topic = {c.get("topic"): c for c in existing.get("cards", []) if isinstance(c, dict)}
        for card in tk.get("cards") or []:
            if isinstance(card, dict) and card.get("topic") in by_topic:
                by_topic[card["topic"]]["title"] = _s(card.get("title"), 60)
                by_topic[card["topic"]]["text"] = _s(card.get("text"), 300)
    elif isinstance(tk, list):  # legacy bullets -> headline/thesis
        bullets = _list(tk, 3)
        existing["headline"] = bullets[0] if bullets else existing.get("headline", "")
        existing["thesis"] = " ".join(bullets[1:]) if len(bullets) > 1 else existing.get("thesis", "")
    existing.setdefault("cards", [])
    block["takeaways"] = existing

    it = raw.get("industry_trends") or {}
    sb["industry_trends"]["headline"] = _s(it.get("headline"))
    by_name = {r["name"]: r for r in sb["industry_trends"]["top"]}
    for d in it.get("drivers") or []:
        if isinstance(d, dict) and d.get("name") in by_name:
            by_name[d["name"]]["drivers"] = _list(d.get("drivers"), 2)

    tt = raw.get("trend_tracking") or {}
    sb["trend_tracking"]["headline"] = _s(tt.get("headline"))
    rows = {r["category"]: r for s in sb["trend_tracking"]["slides"] for r in s["rows"]}
    for i in tt.get("insights") or []:
        if isinstance(i, dict) and i.get("category") in rows:
            rows[i["category"]]["insight"] = _s(i.get("insight"), 240)

    vt = raw.get("volume_trendline") or {}
    sb["volume_trendline"]["headline"] = _s(vt.get("headline"))
    sb["volume_trendline"]["drivers"] = _list(vt.get("drivers"), 3)

    kt = raw.get("key_themes") or {}
    sb["key_themes"]["headline"] = _s(kt.get("headline"))
    by_theme = {i["theme"]: i for i in sb["key_themes"]["insights"]}
    for i in kt.get("insights") or []:
        if isinstance(i, dict) and i.get("theme") in by_theme:
            by_theme[i["theme"]]["text"] = _s(i.get("text"), 400)

    vu = raw.get("voice_of_user") or {}
    sb["voice_of_user"]["headline"] = _s(vu.get("headline"), 900)
    by_brand = {b["name"]: b for b in sb["voice_of_user"]["top_brands"]}
    for b in vu.get("brands") or []:
        if isinstance(b, dict) and b.get("name") in by_brand:
            by_brand[b["name"]]["bullets"] = _list(b.get("bullets"), 3)
    by_region = {r["region"]: r for r in sb["voice_of_user"]["regional_prefs"]}
    for r in vu.get("regional") or []:
        if isinstance(r, dict) and r.get("region") in by_region:
            by_region[r["region"]]["driver"] = _s(r.get("driver"), 240)

    ba = raw.get("brand_analysis") or {}
    sb["brand_analysis"]["global_paragraphs"] = _list(ba.get("global_paragraphs"), 4, 500)
    sb["brand_analysis"]["takeaways"] = _list(ba.get("takeaways"), 2)
    by_brand = {b["name"]: b for b in sb["brand_analysis"]["brands"]}
    for b in ba.get("brands") or []:
        if isinstance(b, dict) and b.get("name") in by_brand:
            by_brand[b["name"]]["summary"] = _s(b.get("summary"))

    by_region = {r["name"]: r for r in sb["regional"]["regions"]}
    for r in raw.get("regional") or []:
        if isinstance(r, dict) and r.get("name") in by_region:
            by_region[r["name"]]["summary"] = _s(r.get("summary"), 400)
            by_region[r["name"]]["key_insights"] = _list(r.get("key_insights"), 3)


# ── network_map ───────────────────────────────────────────────────────────


def _facts_network(sb: dict) -> dict:
    return {
        "brand": sb["meta"]["brand"],
        "competitors": sb["meta"]["competitors"][:6],
        "window": sb["meta"]["window_label"],
        "total_articles": sb["meta"]["total_conversations"],
        "communities": [
            {
                "id": c["id"],
                "name": c["name"],
                "share": c["share"],
                "positive_rate": c["positive_rate"],
                "negative_rate": c["negative_rate"],
                "topics": [t["name"] for t in c["topics"][:3]],
                "rivals": c["rivals"][:2],
                "members": c["members"][:3],
            }
            for c in sb["communities"]
        ],
        "network_stats": sb["network_stats"],
        "spotlight": (
            {k: sb["spotlight"][k] for k in ("handle", "community", "kpis")} if sb.get("spotlight") else None
        ),
    }


_SCHEMA_NETWORK = {
    "hero": {"title": "str ≤ 12 words", "subtitle": "str 1-2 sentences", "chips": ["3-4 short stat chips"]},
    "slides": [{"id": "s1|s2|s3", "title": "str", "body": "str 2-3 sentences"}],
    "communities": [{"id": "int id from facts", "subtitle": "str ≤ 12 words", "insights": ["2-3 bullets"]}],
    "spotlight": {"role": "str ≤ 6 words", "bio": "str 1-2 sentences", "implication": "str 1 sentence"},
    "callout": "str 1-2 sentences",
    "footer": ["2-3 one-line notes incl. that communities are derived from outlet sections/themes"],
}


def _apply_network(sb: dict, raw: dict) -> None:
    hero = raw.get("hero") or {}
    sb["hero"]["title"] = _s(hero.get("title"), 120)
    sb["hero"]["subtitle"] = _s(hero.get("subtitle"), 400)
    sb["hero"]["chips"] = _list(hero.get("chips"), 4, 60)
    slides = {s["id"]: s for s in sb["slides"]}
    for s in raw.get("slides") or []:
        if isinstance(s, dict) and s.get("id") in slides:
            slides[s["id"]]["title"] = _s(s.get("title"), 140)
            slides[s["id"]]["body"] = _s(s.get("body"))
    comms = {c["id"]: c for c in sb["communities"]}
    for c in raw.get("communities") or []:
        if not isinstance(c, dict):
            continue
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        if cid in comms:
            comms[cid]["subtitle"] = _s(c.get("subtitle"), 100)
            comms[cid]["insights"] = _list(c.get("insights"), 3)
    spot = raw.get("spotlight") or {}
    if sb.get("spotlight") and isinstance(spot, dict):
        sb["spotlight"]["role"] = _s(spot.get("role"), 60)
        sb["spotlight"]["bio"] = _s(spot.get("bio"), 400)
        sb["spotlight"]["implication"] = _s(spot.get("implication"), 300)
    sb["callout"] = _s(raw.get("callout"))
    sb["footer"] = _list(raw.get("footer"), 3, 200)


# ── dispatch ──────────────────────────────────────────────────────────────

# ── track_emerging_issues ─────────────────────────────────────────────────

_TONES = {"pos", "neg", "neu"}


def _facts_issues(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"],
        "competitors": meta["competitors"][:6],
        "window": meta["window"],
        "platforms": meta["platforms"],
        "total_mentions": meta["total_mentions"],
        "issue": meta.get("issue"),
        "theme_groups": [{"name": g["name"], "bucket": g.get("bucket"), "count": g.get("count"), "raw_examples": g.get("raw", [])[:6]} for g in (meta.get("taxonomy") or {}).get("groups", [])],
        "trend": {"grain": sb["trend"].get("grain"), "points": sb["trend"]["points"], "annotations": [{"at": a["at"], "kind": a["kind"], "label": a["label"], "value": a["value"]} for a in sb["trend"]["annotations"]]},
        "issue_platform_split": sb["behaviour"].get("platform_split", []),
        "banner_stats": {"journey": sb["journey"]["banner"]["stats"], "behaviour": sb["behaviour"]["banner"]["stats"], "themes": sb["themes"]["banner"]["stats"]},
        "themes": {"funnel": sb["themes"]["funnel"], "rows": [{"name": r["name"], "pct": r["pct"], "count": r["count"]} for r in sb["themes"]["rows"]]},
        "motivation": {"basis": sb["motivation"].get("basis"), "split": sb["motivation"]["split"]},
        "multi": {
            "holders": sb["multi"].get("holders", []),
            "sentiment": sb["multi"].get("sentiment", []),
            "single": {"rows": sb["multi"].get("single", {}).get("rows", []), "quote": sb["multi"].get("single", {}).get("quote")},
            "multiple": {"rows": sb["multi"].get("multiple", {}).get("rows", []), "quote": sb["multi"].get("multiple", {}).get("quote")},
        },
        "evidence": {
            "issue_quotes": [{"text": q["text"][:220], "source": q["source"]} for q in ev.get("issue_quotes", [])],
            "brand_quotes": [{"text": q["text"][:220], "source": q["source"]} for q in ev.get("brand_quotes", [])],
            "unmet_needs": ev.get("unmet_needs", []),
            "product_mentions": ev.get("product_mentions", []),
        },
    }


_SCHEMA_ISSUES = {
    "issue_name": "str ≤ 6 words naming the emerging issue (from facts.issue.group and evidence)",
    "journey": {
        "headline": "str ≤ 12 words",
        "sub": "str 1-2 sentences",
        "stages": [
            {
                "label": "str 2-4 words, lifecycle stage the audience moves through",
                "steps": [{"title": "str 2-5 words", "detail": "str ≤ 25 words, optional, cite real products/brands/platforms from facts"}],
                "gate": "bool, true only for a decision stage",
                "outcomes": [{"label": "str", "tone": "pos|neg|neu"}],
                "note": "str, optional",
            }
        ],
        "_rules": "3 to 5 stages, ordered from discovery to outcome; steps 2-4 per stage; a gate stage has outcomes",
    },
    "trend": {"title": "str e.g. 'Mention trendline · <issue>'", "headline": "str one-sentence read of the curve", "annotations": [{"at": "int from facts.trend.annotations", "text": "str ≤ 20 words"}]},
    "behaviour": {
        "headline": "str ≤ 12 words",
        "sub": "str 1-2 sentences",
        "profile": [{"title": "Behaviour|Interests|Attitude", "sub": "str ≤ 10 words", "points": ["2-3 bullets ≤ 15 words"]}],
        "usage_note": "str 1 sentence",
        "usage": [{"title": "str 2-5 words", "segments": ["str 1-2 audience segments"], "points": ["2-3 bullets"]}],
        "_rules": "exactly 3 profile cards in that order; exactly 4 usage cards grounded in product_mentions/platforms",
    },
    "themes": {"headline": "str ≤ 12 words", "sub": "str 1-2 sentences", "rows": [{"name": "name from facts.themes.rows", "text": "str one sentence on what that theme says"}]},
    "motivation": {"note": "str 1 sentence", "drivers": [{"title": "name from facts.motivation.split", "text": "str one sentence"}]},
    "multi": {"note": "str 1 sentence", "single_points": ["2-3 bullets"], "multiple_points": ["2-3 bullets"]},
}


def _apply_issues(sb: dict, raw: dict) -> None:
    issue_name = _s(raw.get("issue_name"), 60)
    if issue_name:
        sb["meta"].setdefault("issue", {})
        if isinstance(sb["meta"]["issue"], dict):
            sb["meta"]["issue"]["name"] = issue_name

    j = raw.get("journey") or {}
    sb["journey"]["banner"]["headline"] = _s(j.get("headline"), 120)
    sb["journey"]["banner"]["sub"] = _s(j.get("sub"), 400)
    stages = []
    for st in (j.get("stages") or [])[:5]:
        if not isinstance(st, dict) or not _s(st.get("label"), 60):
            continue
        stage = {"label": _s(st.get("label"), 60)}
        steps = [
            {"title": _s(s.get("title"), 80), **({"detail": _s(s.get("detail"), 240)} if _s(s.get("detail")) else {})}
            for s in (st.get("steps") or []) if isinstance(s, dict) and _s(s.get("title"))
        ][:4]
        if steps:
            stage["steps"] = steps
        if st.get("gate") is True:
            stage["gate"] = True
            outcomes = [
                {"label": _s(o.get("label"), 40), "tone": o.get("tone") if o.get("tone") in _TONES else "neu"}
                for o in (st.get("outcomes") or []) if isinstance(o, dict) and _s(o.get("label"))
            ][:4]
            if outcomes:
                stage["outcomes"] = outcomes
        if _s(st.get("note")):
            stage["note"] = _s(st.get("note"), 240)
        stages.append(stage)
    if len(stages) >= 3:
        sb["journey"]["stages"] = stages
        sb["journey"]["banner"]["stats"][0]["value"] = str(len(stages))

    t = raw.get("trend") or {}
    sb["trend"]["title"] = _s(t.get("title"), 120) or (f"Mention trendline · {issue_name}" if issue_name else "Mention trendline")
    sb["trend"]["headline"] = _s(t.get("headline"), 240)
    by_at = {a["at"]: a for a in sb["trend"]["annotations"]}
    for item in t.get("annotations") or []:
        if isinstance(item, dict) and item.get("at") in by_at:
            by_at[item["at"]]["text"] = _s(item.get("text"), 160)
    sb["trend"]["annotations"] = [a for a in sb["trend"]["annotations"] if a.get("text")]

    b = raw.get("behaviour") or {}
    sb["behaviour"]["banner"]["headline"] = _s(b.get("headline"), 120)
    sb["behaviour"]["banner"]["sub"] = _s(b.get("sub"), 400)
    by_title = {p["title"].lower(): p for p in sb["behaviour"]["profile"]}
    for item in b.get("profile") or []:
        if isinstance(item, dict) and str(item.get("title") or "").lower() in by_title:
            card = by_title[str(item["title"]).lower()]
            card["sub"] = _s(item.get("sub"), 80)
            card["points"] = _list(item.get("points"), 3, 160)
    sb["behaviour"]["usage_note"] = _s(b.get("usage_note"), 240)
    sb["behaviour"]["usage"] = [
        {"title": _s(u.get("title"), 60), "segments": _list(u.get("segments"), 2, 60), "points": _list(u.get("points"), 3, 160)}
        for u in (b.get("usage") or []) if isinstance(u, dict) and _s(u.get("title"))
    ][:4]

    th = raw.get("themes") or {}
    sb["themes"]["banner"]["headline"] = _s(th.get("headline"), 120)
    sb["themes"]["banner"]["sub"] = _s(th.get("sub"), 400)
    rows = {r["name"]: r for r in sb["themes"]["rows"]}
    for item in th.get("rows") or []:
        if isinstance(item, dict) and item.get("name") in rows:
            rows[item["name"]]["text"] = _s(item.get("text"), 200)

    m = raw.get("motivation") or {}
    sb["motivation"]["note"] = _s(m.get("note"), 240)
    drivers = {d["title"]: d for d in sb["motivation"]["drivers"]}
    for item in m.get("drivers") or []:
        if isinstance(item, dict) and item.get("title") in drivers:
            drivers[item["title"]]["text"] = _s(item.get("text"), 200)

    mu = raw.get("multi") or {}
    sb["multi"]["note"] = _s(mu.get("note"), 240)
    if "single" in sb["multi"]:
        sb["multi"]["single"]["points"] = _list(mu.get("single_points"), 3, 160)
    if "multiple" in sb["multi"]:
        sb["multi"]["multiple"]["points"] = _list(mu.get("multiple_points"), 3, 160)


# ── shifting_audience_priorities ──────────────────────────────────────────


def _facts_priorities(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"],
        "competitors": meta["competitors"][:6],
        "top_competitor": meta.get("top_competitor"),
        "window": meta["window"],
        "grain": meta["grain"],
        "total_mentions": meta["total_mentions"],
        "method": meta.get("method"),
        "bucket_shares_pct": meta.get("bucket_shares"),
        "theme_groups": [{"name": g["name"], "bucket": g.get("bucket"), "count": g.get("count"), "raw_examples": g.get("raw", [])[:5]} for g in (meta.get("taxonomy") or {}).get("groups", [])],
        "loyalty": {"stats": sb["loyalty"]["banner"]["stats"], "index": {k: sb["loyalty"]["index"][k] for k in ("value", "prior", "min", "max")}, "tracking": sb["loyalty"]["tracking"]},
        "trend": {"points": sb["trend"]["points"], "spikes": [{"at": s["at"], "label": s["label"], "ratio": s["ratio"], "evidence": s.get("evidence")} for s in sb["trend"]["spikes"]], "stats": sb["trend"]["banner"]["stats"]},
        "monthly": {"score": sb["monthly"]["score"], "scale": f"{sb['monthly']['min']}-{sb['monthly']['max']}", "band": sb["monthly"].get("band"), "bands": sb["monthly"]["bands"]},
        "benchmark": sb["benchmark"],
        "evidence": {
            "brand_quotes": [{"text": q["text"][:220], "source": q["source"]} for q in ev.get("brand_quotes", [])],
            "competitor_quotes": [{"text": q["text"][:220], "source": q["source"]} for q in ev.get("competitor_quotes", [])],
            "top_groups": ev.get("top_groups", []),
        },
    }


_SCHEMA_PRIORITIES = {
    "category": "str ≤ 5 words naming the product category the brand competes in",
    "loyalty": {
        "headline": "str ≤ 12 words",
        "sub": "str 1-2 sentences",
        "index_note": "str one sentence on what drives the index value",
        "lead": "str 2-3 sentences explaining how the loyalty read is derived for this category; <b>…</b> allowed for key terms",
        "params": [{"key": "nps|sentiment|usage|switch", "text": "str one sentence tailoring the parameter to this category"}],
    },
    "trend": {
        "headline": "str ≤ 12 words",
        "sub": "str 1-2 sentences",
        "note": "str 1-2 sentences reading the curve",
        "spikes": [{"at": "int from facts.trend.spikes", "label": "str ≤ 6 words naming what drove that period (from its evidence)", "text": "str ≤ 25 words"}],
    },
    "monthly_text": "str 1-2 sentences; must name the band from facts.monthly.band (weak/moderate/strong) and explain the score against the benchmark rows — never call a 'weak' band 'upper' or 'strong'",
}


def _apply_priorities(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _s(raw.get("category"), 60)
    lo = raw.get("loyalty") or {}
    sb["loyalty"]["banner"]["headline"] = _s(lo.get("headline"), 120)
    sb["loyalty"]["banner"]["sub"] = _s(lo.get("sub"), 400)
    sb["loyalty"]["index"]["note"] = _s(lo.get("index_note"), 200)
    sb["loyalty"]["lead"] = _s(lo.get("lead"), 600)
    by_key = {p["key"]: p for p in sb["loyalty"]["params"]}
    for item in lo.get("params") or []:
        if isinstance(item, dict) and item.get("key") in by_key:
            by_key[item["key"]]["text"] = _s(item.get("text"), 200)

    tr = raw.get("trend") or {}
    sb["trend"]["banner"]["headline"] = _s(tr.get("headline"), 120)
    sb["trend"]["banner"]["sub"] = _s(tr.get("sub"), 400)
    sb["trend"]["note"] = _s(tr.get("note"), 300)
    by_at = {s["at"]: s for s in sb["trend"]["spikes"]}
    for item in tr.get("spikes") or []:
        if isinstance(item, dict) and item.get("at") in by_at:
            if _s(item.get("label")):
                by_at[item["at"]]["label"] = _s(item.get("label"), 60)
            by_at[item["at"]]["text"] = _s(item.get("text"), 200)
    sb["monthly"]["text"] = _s(raw.get("monthly_text"), 300)


# ── perception_analysis ───────────────────────────────────────────────────

_MARK_OK = re.compile(r"<(?!/?mark>)[^>]*>")   # strip every tag except <mark>…</mark>


def _rich(value, limit: int = 600) -> str:
    """Like _s but keeps <mark> highlights (contract: no other markup)."""
    text = _MARK_OK.sub("", str(value or "")).strip()
    return text[:limit]


_ANY_TAG = re.compile(r"<[^>]+>")


def _plain(value, limit: int = 600) -> str:
    """For fields the contract marks plain: drop every tag, <mark> included,
    since those screens render the string as text and would show the tags."""
    return _s(_ANY_TAG.sub("", str(value or "")), limit)


def _facts_perception(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"],
        "competitors": meta["competitors"][:6],
        "window": meta["window"],
        "total_mentions": meta["total_mentions"],
        "rated_mentions": meta["rated_mentions"],
        "perception": {
            "stats": sb["perception"]["banner"]["stats"],
            "themes": [{"key": t["key"], "title": t["title"], "pct": t["pct"], "count": t["count"], "brands": t.get("brands", []), "sample_posts": ev.get("themes", {}).get(t["key"], [])} for t in sb["perception"]["themes"]],
            "unclassified": meta.get("classification", {}).get("unclassified"),
        },
        "sentiment": {
            "stats": sb["sentiment"]["banner"]["stats"],
            "split": sb["sentiment"]["split"],
            "groups": [{"tone": g["tone"], "label": g["label"], "pct": g["pct"], "count": g["count"], "drivers": [{"raw_theme": d["raw_theme"], "count": d["count"], "brands": d.get("brands", []), "sample_posts": ev.get("drivers", {}).get(f"{g['tone']}:{d['raw_theme']}", [])} for d in g["drivers"]]} for g in sb["sentiment"]["groups"]],
            "quotes": sb["sentiment"]["quotes"],
        },
        "emotion": {
            "stats": sb["emotion"]["banner"]["stats"],
            "mix": sb["emotion"]["mix"],
            "aspects": [{"key": a["key"], "title": a["title"], "pct": a["pct"], "count": a["count"], "sample_posts": ev.get("aspects", {}).get(a["key"], [])} for a in sb["emotion"]["aspects"]],
            "negative_reasons": ev.get("negative_reasons", []),
        },
    }


_SCHEMA_PERCEPTION = {
    "category": "str ≤ 5 words naming the product category",
    "perception": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence describing the section",
        "summary": "str one paragraph: the overall read of how the audience perceives the category and brand",
        "keywords": ["4-6 short phrases lifted from the summary"],
        "themes": [{"key": "key from facts", "title": "str: keep or re-word for this category, ≤ 4 words", "text": "str 1-3 sentences, rich: wrap 2-4 key phrases in <mark>…</mark>; say less when sample_posts are few; empty string when count is 0"}],
    },
    "sentiment": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "groups": [{"tone": "pos|neu|neg", "drivers": [{"raw_theme": "raw_theme from facts", "title": "str ≤ 4 words", "text": "str one sentence, rich allowed"}]}],
    },
    "emotion": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "summary": "str one paragraph grounded in mix and aspects; if negatives are near zero, say so plainly",
        "lead": "str one sentence, rich: 1-3 <mark> phrases",
        "aspects": [{"key": "key from facts", "title": "str ≤ 4 words", "text": "str 1-2 sentences; empty string when count is 0"}],
    },
}


def _apply_perception(sb: dict, raw: dict) -> None:
    # Contract §3: only fields marked rich may carry <mark>; everything else is
    # rendered as plain text by the screen, so tags are stripped here.
    sb["meta"]["category"] = _plain(raw.get("category"), 60)

    p = raw.get("perception") or {}
    sb["perception"]["banner"]["headline"] = _plain(p.get("headline"), 100)
    sb["perception"]["banner"]["sub"] = _plain(p.get("sub"), 400)
    sb["perception"]["note"] = _plain(p.get("note"), 240)
    sb["perception"]["summary"] = _plain(p.get("summary"), 900)
    sb["perception"]["keywords"] = [_plain(k, 60) for k in _list(p.get("keywords"), 6, 80) if _plain(k)]
    by_key = {t["key"]: t for t in sb["perception"]["themes"]}
    for item in p.get("themes") or []:
        if isinstance(item, dict) and item.get("key") in by_key:
            card = by_key[item["key"]]
            if _plain(item.get("title")):
                card["title"] = _plain(item.get("title"), 48)
            card["text"] = _rich(item.get("text"), 420) if card["count"] else ""

    s = raw.get("sentiment") or {}
    sb["sentiment"]["banner"]["headline"] = _plain(s.get("headline"), 100)
    sb["sentiment"]["banner"]["sub"] = _plain(s.get("sub"), 400)
    sb["sentiment"]["note"] = _plain(s.get("note"), 240)
    groups = {g["tone"]: g for g in sb["sentiment"]["groups"]}
    for item in s.get("groups") or []:
        if not isinstance(item, dict) or item.get("tone") not in groups:
            continue
        drivers = {d["raw_theme"]: d for d in groups[item["tone"]]["drivers"]}
        for d in item.get("drivers") or []:
            if isinstance(d, dict) and d.get("raw_theme") in drivers:
                target = drivers[d["raw_theme"]]
                if _plain(d.get("title")):
                    target["title"] = _plain(d.get("title"), 48)
                target["text"] = _rich(d.get("text"), 300)

    e = raw.get("emotion") or {}
    sb["emotion"]["banner"]["headline"] = _plain(e.get("headline"), 100)
    sb["emotion"]["banner"]["sub"] = _plain(e.get("sub"), 400)
    sb["emotion"]["note"] = _plain(e.get("note"), 240)
    sb["emotion"]["summary"] = _plain(e.get("summary"), 900)
    sb["emotion"]["lead"] = _rich(e.get("lead"), 300)
    aspects = {a["key"]: a for a in sb["emotion"]["aspects"]}
    for item in e.get("aspects") or []:
        if isinstance(item, dict) and item.get("key") in aspects:
            card = aspects[item["key"]]
            if _plain(item.get("title")):
                card["title"] = _plain(item.get("title"), 48)
            card["text"] = _rich(item.get("text"), 300) if card["count"] else ""

    # Drop internal-only fields the screen never reads.
    for g in sb["sentiment"]["groups"]:
        for d in g["drivers"]:
            d.pop("raw_theme", None)


# ── dominant_narratives ───────────────────────────────────────────────────


def _facts_narratives(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"],
        "competitors": meta["competitors"][:6],
        "window": meta["window"],
        "total_mentions": meta["total_mentions"],
        "usage": {
            "stats": sb["usage"]["banner"]["stats"],
            "groups": [{"key": g["key"], "title": g["title"], "count": g["count"], "sample_posts": ev.get("usage", {}).get(g["key"], [])} for g in sb["usage"]["groups"]],
        },
        "observations": {
            "columns": [
                {
                    "key": c["key"],
                    "q": c["q"],
                    "count": c.get("count", 0),
                    "reco": c.get("reco", False),
                    # The recommendations column synthesises the other four, so it sees their evidence.
                    "sample_posts": (
                        [p for k in ("q1", "q2", "q3", "q4") for p in ev.get("questions", {}).get(k, [])[:3]]
                        if c.get("reco") else ev.get("questions", {}).get(c["key"], [])
                    ),
                }
                for c in sb["observations"]["columns"]
            ],
        },
        "landscape": {
            "stats": sb["landscape"]["banner"]["stats"],
            "platforms": sb["landscape"]["platforms"],
            "issuers": sb["landscape"]["issuers"],
            "callouts": [{"key": c["key"], "title": c["title"], "count": c["count"]} for c in sb["landscape"]["callouts"]],
            "top_issuer_posts": ev.get("top_issuer_posts", []),
            "award_posts": ev.get("award_posts", []),
            "positive_brand_posts": ev.get("positive_brand_posts", []),
            "positive_brand_count": sb["landscape"].get("positive_brand_posts", 0),
            "brand_quotes": ev.get("brand_quotes", []),
        },
        "outlook": {
            "stat_candidates": sb["outlook"].get("stat_candidates", []),
            "themes": [{"key": t["key"], "title": t["title"], "pct": t["pct"], "count": t["count"], "sample_posts": ev.get("outlook", {}).get(t["key"], [])} for t in sb["outlook"]["themes"]],
            "tagged_posts": sb["outlook"].get("tagged_posts", 0),
        },
    }


_SCHEMA_NARRATIVES = {
    "category": "str ≤ 5 words naming the product category",
    "usage": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "summary": "str one paragraph on how the category is used and regarded",
        "keywords": ["4-6 short phrases lifted from the summary"],
        "groups": [{"key": "patterns|engagement|perception", "points": ["2-3 bullets, 1-2 sentences each, rich: wrap 2-4 key phrases in <mark>…</mark>; fewer bullets when sample_posts are few; none when count is 0"]}],
    },
    "observations": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "columns": [{"key": "q1|q2|q3|q4|reco", "q": "str: the question re-worded for this category, keep the intent", "points": ["1-2 bullets, rich allowed; empty list only when a q1-q4 column has count 0"]}],
        "_rules": "the reco column must always carry 2-3 recommendation bullets synthesised from the other four columns' sample_posts, even though its own count is 0",
    },
    "landscape": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "sec_title": "str ≤ 10 words",
        "note": "str one sentence",
        "callouts": [{"key": "volume|award", "title": "str ≤ 8 words: for volume the most-named product or line of the top brand from top_issuer_posts, else the brand name; for award what the recognition is", "text": "str one sentence"}],
        "goods_title": "str ≤ 10 words, e.g. '<brand> · what is going well'",
        "goods_lead": "str one sentence",
        "goods": [{"key": "rewards|fee|benefit|award", "title": "str ≤ 5 words", "text": "str one sentence grounded in positive_brand_posts"}],
        "_rules": "goods: 3-5 cards only when positive_brand_count >= 3, else an empty list",
    },
    "outlook": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "sec_title": "str ≤ 8 words",
        "note": "str one sentence",
        "stats": [{"value": "value copied exactly from stat_candidates", "label": "its label, may be shortened"}],
        "themes": [{"key": "key from facts", "title": "str ≤ 5 words, may refine the given title", "text": "str 1-2 sentences grounded in sample_posts"}],
        "_rules": "stats: pick exactly 3 from stat_candidates",
    },
}

_GOODS_KEYS = {"rewards", "fee", "benefit", "award"}


def _points(items, limit: int) -> list[dict]:
    out = []
    for p in (items or [])[:limit]:
        text = _rich(p.get("text") if isinstance(p, dict) else p, 300)
        if text:
            out.append({"text": text})   # never ext: true — no secondary-research input exists
    return out


def _apply_narratives(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)

    u = raw.get("usage") or {}
    sb["usage"]["banner"]["headline"] = _plain(u.get("headline"), 100)
    sb["usage"]["banner"]["sub"] = _plain(u.get("sub"), 400)
    sb["usage"]["note"] = _plain(u.get("note"), 240)
    sb["usage"]["summary"] = _plain(u.get("summary"), 900)
    sb["usage"]["keywords"] = [_plain(k, 60) for k in _list(u.get("keywords"), 6, 80) if _plain(k)]
    groups = {g["key"]: g for g in sb["usage"]["groups"]}
    for item in u.get("groups") or []:
        if isinstance(item, dict) and item.get("key") in groups and groups[item["key"]]["count"]:
            groups[item["key"]]["points"] = _points(item.get("points"), 3)

    o = raw.get("observations") or {}
    sb["observations"]["banner"]["headline"] = _plain(o.get("headline"), 100)
    sb["observations"]["banner"]["sub"] = _plain(o.get("sub"), 400)
    sb["observations"]["note"] = _plain(o.get("note"), 240)
    cols = {c["key"]: c for c in sb["observations"]["columns"]}
    for item in o.get("columns") or []:
        if not isinstance(item, dict) or item.get("key") not in cols:
            continue
        col = cols[item["key"]]
        if _plain(item.get("q")):
            col["q"] = _plain(item.get("q"), 120)
        if col.get("reco") or col.get("count"):
            col["points"] = [p["text"] for p in _points(item.get("points"), 3)]

    l = raw.get("landscape") or {}
    sb["landscape"]["banner"]["headline"] = _plain(l.get("headline"), 100)
    sb["landscape"]["banner"]["sub"] = _plain(l.get("sub"), 400)
    sb["landscape"]["sec_title"] = _plain(l.get("sec_title"), 100)
    sb["landscape"]["note"] = _plain(l.get("note"), 240)
    callouts = {c["key"]: c for c in sb["landscape"]["callouts"]}
    for item in l.get("callouts") or []:
        if isinstance(item, dict) and item.get("key") in callouts:
            if _plain(item.get("title")):
                callouts[item["key"]]["title"] = _plain(item.get("title"), 80)
            callouts[item["key"]]["text"] = _plain(item.get("text"), 200)
    sb["landscape"]["callouts"] = [c for c in sb["landscape"]["callouts"] if c["title"] and c["text"]]
    if sb["landscape"].get("positive_brand_posts", 0) >= 3:
        sb["landscape"]["goods_title"] = _plain(l.get("goods_title"), 100)
        sb["landscape"]["goods_lead"] = _plain(l.get("goods_lead"), 240)
        sb["landscape"]["goods"] = [
            {"key": g["key"], "title": _plain(g.get("title"), 48), "text": _plain(g.get("text"), 220)}
            for g in (l.get("goods") or [])[:5]
            if isinstance(g, dict) and g.get("key") in _GOODS_KEYS and _plain(g.get("title")) and _plain(g.get("text"))
        ]

    ok = raw.get("outlook") or {}
    sb["outlook"]["banner"]["headline"] = _plain(ok.get("headline"), 100)
    sb["outlook"]["banner"]["sub"] = _plain(ok.get("sub"), 400)
    sb["outlook"]["sec_title"] = _plain(ok.get("sec_title"), 80)
    sb["outlook"]["note"] = _plain(ok.get("note"), 240)
    allowed = {c["value"]: c for c in sb["outlook"].get("stat_candidates", [])}
    picked = []
    for s in ok.get("stats") or []:
        if isinstance(s, dict) and str(s.get("value")) in allowed and str(s.get("value")) not in {p["value"] for p in picked}:
            picked.append({"value": str(s["value"]), "label": _plain(s.get("label"), 40) or allowed[str(s["value"])]["label"]})
    if len(picked) == 3:
        sb["outlook"]["banner"]["stats"] = [sb["outlook"]["banner"]["stats"][0], *picked]
    themes = {t["key"]: t for t in sb["outlook"]["themes"]}
    for item in ok.get("themes") or []:
        if isinstance(item, dict) and item.get("key") in themes:
            if _plain(item.get("title")):
                themes[item["key"]]["title"] = _plain(item.get("title"), 48)
            themes[item["key"]]["text"] = _plain(item.get("text"), 260)

    # Internal-only fields the screen never reads.
    sb["outlook"].pop("stat_candidates", None)
    for g in sb["usage"]["groups"]:
        g.pop("key", None)
    for c in sb["observations"]["columns"]:
        c.pop("key", None)


# ── brand_perception ──────────────────────────────────────────────────────


def _facts_brand_perception(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"],
        "competitors": meta["competitors"][:6],
        "window": meta["window"],
        "total_mentions": meta["total_mentions"],
        "brand_tagged_posts": meta.get("brand_tagged"),
        "perception": {
            "stats": sb["perception"]["banner"]["stats"],
            "popularity": sb["perception"]["popularity"],
            "products": [
                {"name": p["name"], "brand": p["brand"], "count": p["count"], "award_posts": p.get("award_posts", 0), "sample_posts": ev.get("products", {}).get(p["name"], []), "award_sample": ev.get("product_awards", {}).get(p["name"], [])}
                for p in sb["perception"]["products"]
            ],
        },
        "popularity": {
            "brands": [{"name": b["name"], "pct": b["pct"], "is_brand": b.get("is_brand", False), "sample_posts": ev.get("brands", {}).get(b["name"], [])} for b in sb["popularity"]["brands"]],
        },
        "switching": {
            "stats": sb["switching"]["banner"]["stats"],
            "switching_posts": sb["switching"].get("switching_posts", 0),
            "reasons": [{"key": r["key"], "short": r["short"], "pct": r["pct"], "count": r["count"], "sample_posts": ev.get("drivers", {}).get(r["key"], [])} for r in sb["switching"]["reasons"]],
            "sample_posts": ev.get("switching_sample", []),
        },
    }


_SCHEMA_BRAND_PERCEPTION = {
    "category": "str ≤ 5 words naming the product category",
    "perception": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "summary": "str one sentence: what the audience appreciates about the brands",
        "keywords": ["4-6 short phrases lifted from the summary"],
        "products": [{"name": "name from facts", "tags": ["2-4 short attribute tags"], "award": "str ≤ 12 words naming the recognition, ONLY when award_posts > 0 and award_sample supports it, else empty string", "text": "str 1-2 sentences, rich: wrap 2-3 key phrases in <mark>…</mark>"}],
    },
    "popularity": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "lead": "str one sentence, rich: 1-3 <mark> phrases",
        "brands": [{"name": "name from facts", "points": ["1-3 bullets, 1-2 sentences each, rich: 2-3 <mark> phrases; fewer when sample_posts are few"]}],
    },
    "switching": {
        "headline": "str ≤ 10 words",
        "sub": "str 1-2 sentences",
        "note": "str one sentence",
        "reasons": [{"key": "key from facts", "short": "str ≤ 4 words for the chart label", "title": "str ≤ 8 words", "text": "str 1-2 sentences grounded in sample_posts"}],
        "_rules": "when switching_posts is 0 write a sub that says so plainly",
    },
}


def _apply_brand_perception(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)

    p = raw.get("perception") or {}
    sb["perception"]["banner"]["headline"] = _plain(p.get("headline"), 100)
    sb["perception"]["banner"]["sub"] = _plain(p.get("sub"), 400)
    sb["perception"]["note"] = _plain(p.get("note"), 240)
    sb["perception"]["summary"] = _plain(p.get("summary"), 400)
    sb["perception"]["keywords"] = [_plain(k, 60) for k in _list(p.get("keywords"), 6, 80) if _plain(k)]
    cards = {c["name"]: c for c in sb["perception"]["products"]}
    for item in p.get("products") or []:
        if not isinstance(item, dict) or item.get("name") not in cards:
            continue
        card = cards[item["name"]]
        card["tags"] = [_plain(t, 40) for t in _list(item.get("tags"), 4, 60) if _plain(t)]
        card["text"] = _rich(item.get("text"), 300)
        award = _plain(item.get("award"), 120)
        if award and card.get("award_posts"):
            card["award"] = award
    for card in sb["perception"]["products"]:
        card.pop("award_posts", None)
        card.pop("count", None)

    po = raw.get("popularity") or {}
    sb["popularity"]["banner"]["headline"] = _plain(po.get("headline"), 100)
    sb["popularity"]["banner"]["sub"] = _plain(po.get("sub"), 400)
    sb["popularity"]["note"] = _plain(po.get("note"), 240)
    sb["popularity"]["lead"] = _rich(po.get("lead"), 300)
    rows = {b["name"]: b for b in sb["popularity"]["brands"]}
    for item in po.get("brands") or []:
        if isinstance(item, dict) and item.get("name") in rows:
            rows[item["name"]]["points"] = [t for t in (_rich(x, 260) for x in _list(item.get("points"), 3, 300)) if t]

    s = raw.get("switching") or {}
    sb["switching"]["banner"]["headline"] = _plain(s.get("headline"), 100)
    sb["switching"]["banner"]["sub"] = _plain(s.get("sub"), 400)
    sb["switching"]["note"] = _plain(s.get("note"), 240)
    reasons = {r["key"]: r for r in sb["switching"]["reasons"]}
    for item in s.get("reasons") or []:
        if isinstance(item, dict) and item.get("key") in reasons:
            r = reasons[item["key"]]
            if _plain(item.get("short")):
                r["short"] = _plain(item.get("short"), 40)
            if _plain(item.get("title")):
                r["title"] = _plain(item.get("title"), 80)
            r["text"] = _rich(item.get("text"), 300)
    for r in sb["switching"]["reasons"]:
        r.pop("count", None)
    sb["switching"].pop("switching_posts", None)


# ── whitespace & gap: audience_expectation ───────────────────────────────

_WG_RULES = (
    "British spelling, present tense, no marketing tone, no exclamation marks. Never state a number not in FACTS. "
    "Describe only what sample_posts support; where a card has few or no posts write fewer or no bullets. "
    "Rich fields may wrap 2-4 key phrases in <mark>…</mark>; no other markup anywhere."
)


def _facts_expectation(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"], "competitors": meta["competitors"][:6], "window": meta["window"], "total_mentions": meta["total_mentions"],
        "needs": {
            "stats": sb["needs"]["banner"]["stats"],
            "attributes": [{"key": a["key"], "name": a["name"], "pct": a["pct"], "count": a["count"], "sample_posts": ev.get("attributes", {}).get(a["key"], [])} for a in sb["needs"]["attributes"]],
            "usage_posts": sb["needs"].get("usage_posts", 0),
            "usage_sample": ev.get("usage", []),
        },
        "unmet": {"stats": sb["unmet"]["banner"]["stats"], "needs": [{"key": n["key"], "title": n["title"], "pct": n.get("pct"), "count": n["count"], "sample_posts": ev.get("unmet", {}).get(n["key"], [])} for n in sb["unmet"]["needs"]]},
        "digital": {"pillars": [{"key": p["key"], "title": p["title"], "pct": p["pct"], "count": p["count"], "quote": p.get("quote"), "sample_posts": ev.get("pillars", {}).get(p["key"], [])} for p in sb["digital"]["pillars"]], "digital_posts": sb["digital"].get("digital_posts", 0)},
    }


_SCHEMA_EXPECTATION = {
    "category": "str ≤ 5 words naming the product category",
    "needs": {
        "headline": "str ≤ 12 words", "sub": "str 1-2 sentences", "note": "str one sentence",
        "attributes": [{"key": "key from facts", "name": "str ≤ 5 words: keep or re-word the attribute for this category", "points": ["1-3 bullets, rich"]}],
        "drivers_title": "str ≤ 8 words, e.g. 'Key drivers to use <category>'", "drivers_lead": "str one sentence",
        "drivers": ["4-6 bullets on why people use the product, rich, grounded in usage_sample; empty list when usage_posts < 3"],
    },
    "unmet": {"headline": "str ≤ 12 words", "sub": "str 1-2 sentences", "note": "str one sentence", "needs": [{"key": "key from facts", "title": "str ≤ 8 words, may refine", "text": "str 1-2 sentences, rich"}]},
    "digital": {"headline": "str ≤ 12 words", "sub": "str 1-2 sentences; when digital_posts is 0 say plainly that digital complaints are absent", "note": "str one sentence", "pillars": [{"key": "key from facts", "title": "str ≤ 4 words, may refine", "text": "str 1-2 sentences, rich"}]},
}


def _apply_expectation(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)
    n = raw.get("needs") or {}
    sb["needs"]["banner"]["headline"] = _plain(n.get("headline"), 120)
    sb["needs"]["banner"]["sub"] = _plain(n.get("sub"), 400)
    sb["needs"]["note"] = _plain(n.get("note"), 240)
    attrs = {a["key"]: a for a in sb["needs"]["attributes"]}
    for item in n.get("attributes") or []:
        if isinstance(item, dict) and item.get("key") in attrs:
            a = attrs[item["key"]]
            if _plain(item.get("name")):
                a["name"] = _plain(item.get("name"), 48)
            a["points"] = [t for t in (_rich(x, 260) for x in _list(item.get("points"), 3, 300)) if t]
    if sb["needs"].get("usage_posts", 0) >= 3:
        sb["needs"]["drivers_title"] = _plain(n.get("drivers_title"), 80)
        sb["needs"]["drivers_lead"] = _plain(n.get("drivers_lead"), 240)
        sb["needs"]["drivers"] = [t for t in (_rich(x, 260) for x in _list(n.get("drivers"), 6, 300)) if t]
    u = raw.get("unmet") or {}
    sb["unmet"]["banner"]["headline"] = _plain(u.get("headline"), 120)
    sb["unmet"]["banner"]["sub"] = _plain(u.get("sub"), 400)
    sb["unmet"]["note"] = _plain(u.get("note"), 240)
    cards = {c["key"]: c for c in sb["unmet"]["needs"]}
    for item in u.get("needs") or []:
        if isinstance(item, dict) and item.get("key") in cards:
            if _plain(item.get("title")):
                cards[item["key"]]["title"] = _plain(item.get("title"), 80)
            cards[item["key"]]["text"] = _rich(item.get("text"), 300)
    d = raw.get("digital") or {}
    sb["digital"]["banner"]["headline"] = _plain(d.get("headline"), 120)
    sb["digital"]["banner"]["sub"] = _plain(d.get("sub"), 400)
    sb["digital"]["note"] = _plain(d.get("note"), 240)
    pillars = {p["key"]: p for p in sb["digital"]["pillars"]}
    for item in d.get("pillars") or []:
        if isinstance(item, dict) and item.get("key") in pillars:
            if _plain(item.get("title")):
                pillars[item["key"]]["title"] = _plain(item.get("title"), 48)
            pillars[item["key"]]["text"] = _rich(item.get("text"), 300)
    for coll, key in ((sb["needs"]["attributes"], "count"), (sb["unmet"]["needs"], "count"), (sb["digital"]["pillars"], "count")):
        for c in coll:
            c.pop(key, None)
    sb["needs"].pop("usage_posts", None)
    sb["digital"].pop("digital_posts", None)


# ── whitespace & gap: brand_messaging ────────────────────────────────────


def _facts_messaging(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"], "competitors": meta["competitors"][:6], "window": meta["window"], "initiative_posts": meta.get("initiative_posts"),
        "brands": [{"name": b["name"], "is_brand": b["is_brand"], "mentions": b["mentions"], "initiatives": [{"key": i["key"], "title": i["title"], "pct": i["pct"], "count": i["count"], "sample_posts": ev.get(b["name"], {}).get(i["key"], [])} for i in b["initiatives"]]} for b in sb["brands"]],
    }


_SCHEMA_MESSAGING = {
    "category": "str ≤ 5 words naming the product category",
    "note": "str one sentence describing what this view shows",
    "brands": [{"name": "name from facts", "headline": "str ≤ 12 words on what this brand talks about", "sub": "str 1-2 sentences", "initiatives": [{"key": "key from facts", "title": "str ≤ 5 words, may refine", "points": ["1-3 bullets, rich, grounded in sample_posts"]}]}],
}


def _apply_messaging(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)
    sb["note"] = _plain(raw.get("note"), 240)
    brands = {b["name"]: b for b in sb["brands"]}
    for item in raw.get("brands") or []:
        if not isinstance(item, dict) or item.get("name") not in brands:
            continue
        b = brands[item["name"]]
        b["headline"] = _plain(item.get("headline"), 120)
        b["sub"] = _plain(item.get("sub"), 400)
        inits = {i["key"]: i for i in b["initiatives"]}
        for it in item.get("initiatives") or []:
            if isinstance(it, dict) and it.get("key") in inits:
                if _plain(it.get("title")):
                    inits[it["key"]]["title"] = _plain(it.get("title"), 60)
                inits[it["key"]]["points"] = [t for t in (_rich(x, 260) for x in _list(it.get("points"), 3, 300)) if t]
    for b in sb["brands"]:
        for i in b["initiatives"]:
            i.pop("count", None)
            i.pop("key", None)


# ── whitespace & gap: brand_performance ──────────────────────────────────


def _facts_performance(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"], "competitors": meta["competitors"][:6], "window": meta["window"], "total_mentions": meta["total_mentions"],
        "voice": {"stats": sb["voice"]["banner"]["stats"], "share": sb["voice"]["share"], "sentiment": sb["voice"]["sentiment"], "best_competitor": sb["voice"].get("best_competitor"), "brand_posts_sample": {k: v[:4] for k, v in ev.get("brand_posts", {}).items()}},
        "digital": {"stats": sb["digital"]["banner"]["stats"], "brands": [{"name": b["name"], "is_brand": b["is_brand"], "posts": b["posts"], "themes": b["themes"], "positive_sample": ev.get("digital", {}).get(b["name"], {}).get("positive", []), "negative_sample": ev.get("digital", {}).get(b["name"], {}).get("negative", []), "sample_posts": ev.get("digital", {}).get(b["name"], {}).get("all", [])} for b in sb["digital"]["brands"]]},
    }


_SCHEMA_PERFORMANCE = {
    "category": "str ≤ 5 words naming the product category",
    "voice": {"headline": "str ≤ 12 words", "sub": "str 1-2 sentences", "note": "str one sentence", "callout": "str one sentence on the gap between the project brand and best_competitor, using only numbers in FACTS"},
    "digital": {"headline": "str ≤ 12 words", "sub": "str 1-2 sentences; when no brand has digital posts say so plainly", "note": "str one sentence",
                "brands": [{"name": "name from facts", "working": ["2-5 bullets, rich, from positive_sample; empty when positive_sample is empty"], "not_working": ["1-4 bullets, rich, from negative_sample; empty when negative_sample is empty"]}]},
}


def _apply_performance(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)
    v = raw.get("voice") or {}
    sb["voice"]["banner"]["headline"] = _plain(v.get("headline"), 120)
    sb["voice"]["banner"]["sub"] = _plain(v.get("sub"), 400)
    sb["voice"]["note"] = _plain(v.get("note"), 240)
    sb["voice"]["callout"] = _plain(v.get("callout"), 300)
    sb["voice"].pop("best_competitor", None)
    d = raw.get("digital") or {}
    sb["digital"]["banner"]["headline"] = _plain(d.get("headline"), 120)
    sb["digital"]["banner"]["sub"] = _plain(d.get("sub"), 400)
    sb["digital"]["note"] = _plain(d.get("note"), 240)
    brands = {b["name"]: b for b in sb["digital"]["brands"]}
    for item in d.get("brands") or []:
        if isinstance(item, dict) and item.get("name") in brands:
            b = brands[item["name"]]
            b["working"] = [t for t in (_rich(x, 260) for x in _list(item.get("working"), 5, 300)) if t]
            b["not_working"] = [t for t in (_rich(x, 260) for x in _list(item.get("not_working"), 4, 300)) if t]
    for b in sb["digital"]["brands"]:
        b.pop("posts", None)


# ── user_behaviour ────────────────────────────────────────────────────────


def _facts_behaviour(sb: dict) -> dict:
    meta, ev = sb["meta"], sb.get("evidence", {})
    return {
        "brand": meta["brand"], "competitors": meta["competitors"][:6], "window": meta["window"], "total_mentions": meta["total_mentions"],
        "segments": {
            "stats": sb["segments"]["banner"]["stats"],
            "shares_shown": sb["segments"].get("shares_shown"),
            "coverage_pct": sb["segments"].get("coverage_pct"),
            "groups": [{"key": g["key"], "range": g["range"], "title": g["title"], "pct": g.get("pct"), "count": g["count"], "sample_posts": ev.get("segments", {}).get(g["key"], [])} for g in sb["segments"]["groups"]],
        },
        "multi": {
            "stats": sb["multi"]["banner"]["stats"],
            "multi_posts": sb["multi"].get("multi_posts"),
            "brand_choice": sb["multi"]["brand_choice"],
            "reasons": ev.get("reasons", []),
            "rules": ev.get("rules", []),
            "sample_posts": ev.get("multi_sample", []),
        },
    }


_SCHEMA_BEHAVIOUR = {
    "category": "str ≤ 5 words naming the product category",
    "segments": {
        "headline": "str ≤ 12 words", "sub": "str 1-2 sentences", "note": "str one sentence; keep the given note's meaning when shares_shown is false", "lead": "str one sentence, rich",
        "groups": [{"key": "key from facts", "title": "str ≤ 6 words, may refine", "points": ["2-3 bullets, rich, on behaviour, product use and what matters to this segment, grounded in sample_posts"]}],
    },
    "multi": {
        "headline": "str ≤ 12 words", "sub": "str 1-2 sentences; when multi_posts is small say so plainly", "note": "str one sentence",
        "questions": [
            {"key": "why", "q": "str: the question re-worded for this category", "points": ["one bullet per item in facts.multi.reasons, rich, grounded in its sample_posts; empty list when reasons is empty"]},
            {"key": "how", "q": "str: the question re-worded for this category", "points": ["one bullet per item in facts.multi.rules, rich, grounded in its sample_posts; empty list when rules is empty"]},
        ],
    },
}


def _apply_behaviour(sb: dict, raw: dict) -> None:
    sb["meta"]["category"] = _plain(raw.get("category"), 60)
    s = raw.get("segments") or {}
    sb["segments"]["banner"]["headline"] = _plain(s.get("headline"), 120)
    sb["segments"]["banner"]["sub"] = _plain(s.get("sub"), 400)
    if sb["segments"].get("shares_shown"):
        sb["segments"]["note"] = _plain(s.get("note"), 240)
    sb["segments"]["lead"] = _rich(s.get("lead"), 300)
    groups = {g["key"]: g for g in sb["segments"]["groups"]}
    for item in s.get("groups") or []:
        if isinstance(item, dict) and item.get("key") in groups:
            if _plain(item.get("title")):
                groups[item["key"]]["title"] = _plain(item.get("title"), 60)
            groups[item["key"]]["points"] = [t for t in (_rich(x, 260) for x in _list(item.get("points"), 3, 300)) if t]
    m = raw.get("multi") or {}
    sb["multi"]["banner"]["headline"] = _plain(m.get("headline"), 120)
    sb["multi"]["banner"]["sub"] = _plain(m.get("sub"), 400)
    sb["multi"]["note"] = _plain(m.get("note"), 240)
    qs = {q["key"]: q for q in sb["multi"]["questions"]}
    limits = {"why": len(sb.get("evidence", {}).get("reasons", [])), "how": len(sb.get("evidence", {}).get("rules", []))}
    for item in m.get("questions") or []:
        if isinstance(item, dict) and item.get("key") in qs:
            q = qs[item["key"]]
            if _plain(item.get("q")):
                q["q"] = _plain(item.get("q"), 120)
            q["points"] = [t for t in (_rich(x, 260) for x in _list(item.get("points"), max(limits[item["key"]], 0), 300)) if t]
    for g in sb["segments"]["groups"]:
        g.pop("count", None)
    for q in sb["multi"]["questions"]:
        q.pop("key", None)
    for k in ("coverage_pct", "shares_shown"):
        sb["segments"].pop(k, None)
    sb["multi"].pop("multi_posts", None)


_REGISTRY = {
    "trend_intelligence": (_facts_trend, _SCHEMA_TREND, _apply_trend, "Write the Trend Intelligence storyboard copy."),
    "user_behaviour": (_facts_behaviour, _SCHEMA_BEHAVIOUR, _apply_behaviour, "Write the User Behaviour Analysis copy. " + _WG_RULES),
    "audience_expectation": (_facts_expectation, _SCHEMA_EXPECTATION, _apply_expectation, "Write the Audience Expectation copy. " + _WG_RULES),
    "brand_messaging": (_facts_messaging, _SCHEMA_MESSAGING, _apply_messaging, "Write the Brand Messaging copy, one block per brand. " + _WG_RULES),
    "brand_performance": (_facts_performance, _SCHEMA_PERFORMANCE, _apply_performance, "Write the Brand Performance copy. " + _WG_RULES),
    "brand_perception": (
        _facts_brand_perception,
        _SCHEMA_BRAND_PERCEPTION,
        _apply_brand_perception,
        "Write the Brand Perception copy. British spelling, present tense, no marketing tone, no exclamation marks. "
        "Never state a number not in FACTS. Describe only what sample_posts support; say less when a sample is thin. "
        "Rich fields may wrap 2-3 key phrases in <mark>…</mark>; no other markup anywhere.",
    ),
    "dominant_narratives": (
        _facts_narratives,
        _SCHEMA_NARRATIVES,
        _apply_narratives,
        "Write the Dominant Narratives copy. British spelling, present tense, no marketing tone, no exclamation marks. "
        "Never state a number not in FACTS. Describe only what sample_posts support; where a group or question has few or no posts, write fewer or no bullets. "
        "Rich fields may wrap 2-4 key phrases in <mark>…</mark>; no other markup anywhere.",
    ),
    "perception_analysis": (
        _facts_perception,
        _SCHEMA_PERCEPTION,
        _apply_perception,
        "Write the Perception Analysis copy. British spelling, present tense, no marketing tone, no exclamation marks. "
        "Never state a number not in FACTS. Describe only what sample_posts support; when a card has few or no posts, say less or leave its text empty. "
        "Where a field says rich, wrap 2-4 key phrases in <mark>…</mark> and use no other markup.",
    ),
    "track_emerging_issues": (_facts_issues, _SCHEMA_ISSUES, _apply_issues, "Write the Track Emerging Issues storyboard copy. The issue is facts.issue.group; name it plainly, describe the audience journey around it, and ground every claim in the numbers and quotes given."),
    "shifting_audience_priorities": (_facts_priorities, _SCHEMA_PRIORITIES, _apply_priorities, "Write the Shifting Audience Priorities (brand loyalty index) copy. Explain the index in this category's terms and label each spike from its evidence."),
    "brand_competitive_intel": (_facts_bci, _SCHEMA_BCI, _apply_bci, "Write the Brand & Competitive Intelligence copy."),
    "brand_health_storyboard": (_facts_health, _SCHEMA_HEALTH, _apply_health, "Write the Brand Health Tracker copy."),
    "brand_intelligence": (_facts_brand_intel, _SCHEMA_BRAND_INTEL, _apply_brand_intel, "Write the Brand Intelligence category-trends report copy."),
    "market_intelligence": (_facts_market, _SCHEMA_MARKET, _apply_market, "Write the Market Intelligence report copy, one block per lens."),
    "network_map": (_facts_network, _SCHEMA_NETWORK, _apply_network, "Write the Network Map storyboard copy."),
}


async def write_narrative(lens_key: str, storyboard: dict) -> dict:
    """Fill prose fields in place. Returns the storyboard. Never raises."""
    entry = _REGISTRY.get(lens_key)
    if not entry:
        return storyboard
    facts_fn, schema, apply_fn, task = entry
    try:
        raw = await asyncio.wait_for(_ask(facts_fn(storyboard), schema, task), timeout=180)
        apply_fn(storyboard, raw)
        storyboard.setdefault("meta", {})["narrative"] = "ok"
    except Exception as exc:
        logger.warning("CI narrative failed for %s: %s", lens_key, exc)
        storyboard.setdefault("meta", {})["narrative"] = f"failed: {type(exc).__name__}"
    return storyboard
