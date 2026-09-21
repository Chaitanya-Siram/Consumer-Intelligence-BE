"""Congruence & Content Intelligence storyboard — AI/LLM Audit and Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-congruence-content.md
Screen:   Consumer-Intelligence-FE/src/screens/CongruenceContentScreen.jsx

Unlike every other CI lens the primary input is an LLM audit run
(llm_audit.execute_run): a fixed prompt set answered by each available
assistant, with the sources each answer gives. prepare() loads a fresh run or
executes one, proposes narrative pillars when the project has none, and
labels every response (audit_classify). build_storyboard() then computes all
numbers from those responses; tagged articles only cross-check the themes
(social validation). Prose fields are left empty for narrative.py.

Everything the payload asserts about "grounding" is honest: meta.citation_mode
says whether the sources were retrieved by the assistant or merely claimed.
"""

import re
from collections import Counter, defaultdict

from .. import aggregate, brand_media, cohorts, llm_audit, timeseries
from ..audit_classify import label_responses, propose_pillars

LENS_KEY = "congruence_content"
PREPARE_KEY = "llm_audit"
PREPARE_ACCEPTS_CONTEXT = True   # builder passes session_id= and refresh= to prepare()
__all__ = ["LENS_KEY", "PREPARE_KEY", "PREPARE_ACCEPTS_CONTEXT", "build_storyboard", "prepare", "resolve_journalist_photos"]

TABS = [
    {"id": "t1", "label": "Overview"},
    {"id": "t2", "label": "LLM Analysis"},
    {"id": "t3", "label": "LLM Interpretation"},
    {"id": "t4", "label": "Narrative Scan"},
]
# Static framework copy the contract asks to return verbatim (cc-sample.js).
STAGES = [
    {"n": 1, "key": "analysis", "title": "LLM Analysis", "tone": "purple", "questions": ["Who is shaping the conversation about the brand and category inside LLMs?", "Which sources appear most frequently in LLM responses?", "Where does influence concentrate?"], "method": ["Source listing from LLM outputs", "Journalist and outlet identification", "Ranking by frequency, authority and reach"], "deliverables": ["Ranked media source list", "Journalist / reporter influence map", "Network map of narrative drivers"]},
    {"n": 2, "key": "interpretation", "title": "LLM Interpretation Study", "tone": "blue", "questions": ["How is the brand described across LLMs?", "What themes, sentiment and language dominate?", "Where do positioning gaps or risks appear?"], "method": ["Run standardised prompts across multiple LLMs", "Analyse outputs for narrative themes, emotional tone, language cues, positioning alignment"], "deliverables": ["LLM interpretation report", "Theme & sentiment matrices", "Narrative strength and consistency scores"]},
    {"n": 3, "key": "scan", "title": "Brand Narrative Intelligence Scan", "tone": "green", "questions": ["Where does AI perception align with brand intent?", "Where does it diverge?", "Which narratives are being reinforced or diluted?"], "method": ["Cross-channel narrative comparison", "Consistency and differentiation assessment", "Perception-shaping signal analysis"], "deliverables": ["Brand vs. LLM narrative gap analysis", "Consistency / incongruence heat-map", "Opportunity and risk flags"]},
]
INPUTS = ["Sources & Reports", "Category & Cultural Context", "Brand KPIs", "LLM Prompts"]
OUTCOMES = [
    {"title": "LLM Signals", "items": ["Sources", "Journalists", "Publishers"]},
    {"title": "LLM Interpretation", "items": ["Themes", "Sentiment", "Positioning"]},
    {"title": "Narrative Intelligence", "items": ["Alignment", "Risks", "Opportunities"]},
    {"title": "Strategic Outcomes", "items": ["Smarter messaging", "Media prioritisation", "AI-ready brand narrative"]},
]
TOP_SOURCES = 10
TOP_JOURNALISTS = 8
SOURCE_TYPE_ICONS = 3  # outlet icons shown beside each source type
ALIGNED_MIN, DILUTED_MIN, CONTRADICT_SHARE = 60, 30, 25
SOCIAL_GAP_LLM_MIN, SOCIAL_GAP_SOCIAL_MAX = 20, 2


_GENERIC_BYLINE_START = ("staff", "editor", "team", "the ", "contributor", "guest ")
_GENERIC_BYLINE_END = (" staff", " editors", " editor", " team", " desk", " newsroom", " contributors")


def _is_generic_byline(name: str) -> bool:
    """A byline that names a newsroom, not a person ("Autoblog Staff",
    "The Editors"): it is no journalist to rank or to look a photo up for."""
    lowered = name.lower().strip()
    return lowered.startswith(_GENERIC_BYLINE_START) or lowered.endswith(_GENERIC_BYLINE_END)


_BYLINE_SPLIT_RX = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.I)


def _split_bylines(author: str) -> list[str]:
    """One name per journalist in a byline. "Collin Morgan, Brian Silvestro" is
    two people, but it is split only when every part is itself a full name, so
    a "Last, First" byline is left whole rather than cut into two fragments."""
    author = re.sub(r"\s+", " ", str(author or "")).strip()
    parts = [p.strip() for p in _BYLINE_SPLIT_RX.split(author) if p.strip()]
    if len(parts) > 1 and all(" " in p for p in parts):
        return parts
    return [author] if author else []


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40] or "pillar"


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str], session_id: int | None = None, refresh: bool = False, category: str = "") -> dict:
    known = known_brands or ([brand] if brand else [])
    competitors = [k for k in known if k.lower() != (brand or "").lower()]
    # `category` comes from the session's message keywords (e.g. "car care").
    # Section labels are not a category, so no fallback to them; when empty the
    # prompts say "the category" and the narrative names it from the responses.
    category = (category or "").strip()
    run = llm_audit.load_run(session_id) if session_id is not None else None
    if refresh or not llm_audit.run_is_fresh(run, brand, category):
        run = await llm_audit.execute_run(brand=brand, category=category or "the category", competitors=competitors)
        run["category"] = category   # empty stays empty so the narrative can name it from the responses
        run["session_id"] = session_id
        brand_posts = cohorts.brand_articles(articles, brand, known) or articles
        proposed = await propose_pillars(brand, category or "the category", brand_posts)
        run["pillars"] = [{"key": _slug(p["name"]), **p} for p in proposed]
        run["pillars_source"] = "proposed_from_tagged_articles" if proposed else "none"
        if session_id is not None:
            llm_audit.save_run(session_id, run)
    for i, r in enumerate(run.get("responses", [])):
        r.setdefault("id", f"{r['llm']}::{r['prompt_id']}::{i}")
    labels = await label_responses(run.get("responses", []), brand=brand, pillars=run.get("pillars", []))
    return {"run": run, "labels": labels}


def _net(labels: list[dict]) -> int | None:
    if not labels:
        return None
    pos = sum(1 for l in labels if l["sentiment"] == "pos")
    neg = sum(1 for l in labels if l["sentiment"] == "neg")
    return round((pos - neg) * 100 / len(labels))


async def resolve_journalist_photos(storyboard: dict) -> None:
    """Real Muck Rack headshots for the "Journalist / reporter influence" table
    (`analysis.journalists`), set as each row's `photo_url`. Best-effort: a
    journalist with no matching profile keeps the FE's initials avatar."""
    rows = (storyboard.get("analysis") or {}).get("journalists") or []
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)
    await brand_media.resolve_muckrack_photos(by_name)


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    competitors = [k for k in known if k.lower() != (brand or "").lower()]
    prepared = prepared or {}
    run = prepared.get("run") or {}
    labels = (prepared.get("labels") or {}).get("by_id", {})
    theme_meta = (prepared.get("labels") or {}).get("themes") or {}
    tmap, ttitles = theme_meta.get("map") or {}, {t["key"]: t["title"] for t in theme_meta.get("themes", [])}
    responses = run.get("responses", [])
    llms = list(run.get("assistants") or [])
    pillars = run.get("pillars") or []
    n_resp = len(responses)
    window = timeseries.window_label(articles)
    run_day = (run.get("run_at") or "")[:10]

    # ── tab 2: analysis (sources, journalists) ────────────────────────────
    cite_rows = []   # one per (response, source)
    for r in responses:
        for c in r.get("citations", []):
            cite_rows.append({"llm": r["llm"], "prompt_id": r["prompt_id"], "domain": c["domain"], "outlet": c.get("outlet") or c["domain"], "type": c.get("type") or "News & trade press", "author": c.get("author") or "", "title": c.get("title") or ""})
    total_cites = len(cite_rows)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for c in cite_rows:
        by_domain[c["domain"]].append(c)
    ranked_domains = sorted(by_domain, key=lambda d: (-len(by_domain[d]), d))
    sources = []
    for d in ranked_domains[:TOP_SOURCES]:
        rows = by_domain[d]
        name = Counter(c["outlet"] for c in rows).most_common(1)[0][0]
        sources.append({"name": name, "domain": d, "type": Counter(c["type"] for c in rows).most_common(1)[0][0], "mentions": len(rows), "llms": sorted({c["llm"] for c in rows}, key=lambda x: llms.index(x) if x in llms else 99)})
    type_counts = Counter(c["type"] for c in cite_rows)
    # Each type carries its most-cited outlets — from every citation, not just the
    # top-ranked ones — so the FE can show their domain icons beside the label
    # ("News & trade press" is a category, not a brand).
    domain_outlet = {d: Counter(c["outlet"] for c in rows).most_common(1)[0][0] for d, rows in by_domain.items()}
    domains_by_type: dict[str, Counter] = defaultdict(Counter)
    for c in cite_rows:
        domains_by_type[c["type"]][c["domain"]] += 1
    type_outlets = {
        t: [(domain_outlet[d], d) for d, _ in counts.most_common(SOURCE_TYPE_ICONS)]
        for t, counts in domains_by_type.items()
    }
    source_types = [
        {"name": r["name"], "pct": r["pct"], "sources": [name for name, _ in type_outlets.get(r["name"], [])]}
        for r in cohorts.pct_rows(dict(type_counts))
    ] if cite_rows else []
    top5 = sum(len(by_domain[d]) for d in ranked_domains[:5])
    journalists = []
    by_author: dict[str, list[dict]] = defaultdict(list)
    for c in cite_rows:
        for a in _split_bylines(c["author"]):
            if len(a) >= 5 and " " in a and not _is_generic_byline(a):
                by_author[a.title()].append(c)
    for a in sorted(by_author, key=lambda k: (-len(by_author[k]), k))[:TOP_JOURNALISTS]:
        rows = by_author[a]
        journalists.append({"name": a, "outlet": Counter(r["outlet"] for r in rows).most_common(1)[0][0], "mentions": len(rows), "beat": "", "headlines": [r["title"] for r in rows if r["title"]][:4]})
    analysis = {
        "banner": {
            "eyebrow": "LLM Analysis · who shapes the narrative",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{total_cites:,}", "label": "Sources cited"},
                {"value": f"{round(top5 * 100 / total_cites)}%" if total_cites else "—", "label": "Top 5 sources' share"},
                {"value": sources[0]["name"] if sources else "—", "label": "Most-cited outlet"},
                {"value": str(len(journalists)), "label": "Named journalists"},
            ],
        },
        "note": "",
        "source_types": source_types,
        "sources": sources,
        "journalists": journalists,
        "concentration": "",
        "per_llm_types": {a: dict(Counter(c["type"] for c in cite_rows if c["llm"] == a).most_common(3)) for a in llms},
    }

    # ── tab 3: interpretation ─────────────────────────────────────────────
    labs = [labels.get(r["id"], {"sentiment": "neu", "themes": [], "descriptors": [], "pillars": {}}) for r in responses]
    sent_counts = Counter(l["sentiment"] for l in labs)
    sent_rows = cohorts.pct_rows({k: sent_counts.get(k, 0) for k in ("pos", "neg", "neu")}, order=["pos", "neg", "neu"]) if labs else []
    sentiment = {r["name"]: r["pct"] for r in sent_rows}
    theme_resp: dict[str, list[int]] = defaultdict(list)   # theme key -> response indexes
    for i, l in enumerate(labs):
        for t in {tmap.get(x) for x in l["themes"] if tmap.get(x)}:
            theme_resp[t].append(i)
    ranked_themes = sorted(theme_resp, key=lambda k: (-len(theme_resp[k]), k))
    qualifiers: dict[str, Counter] = defaultdict(Counter)
    for i, l in enumerate(labs):
        for x in l["themes"]:
            k = tmap.get(x)
            if k:
                qualifiers[k][x] += 1
    themes = [{"key": k, "name": ttitles.get(k, k.replace("-", " ").title()), "count": len(theme_resp[k]), "sub": [q for q, _ in qualifiers[k].most_common(3) if q.lower() != ttitles.get(k, "").lower()][:3]} for k in ranked_themes]
    matrix_rows = []
    for k in ranked_themes[:6]:
        vals = []
        for a in llms:
            sub = [labs[i] for i in theme_resp[k] if responses[i]["llm"] == a]
            vals.append(_net(sub) if sub else 0)
        matrix_rows.append({"theme": ttitles.get(k, k), "values": vals})
    desc_counts = Counter(d for l in labs for d in l["descriptors"])
    language = [d for d, _ in desc_counts.most_common(10)]
    # scores
    strength = round(len(theme_resp[ranked_themes[0]]) * 100 / n_resp) if ranked_themes and n_resp else None
    consistency = None
    if len(llms) >= 2 and ranked_themes:
        dists = []
        per_llm = {a: Counter() for a in llms}
        for i, l in enumerate(labs):
            for t in {tmap.get(x) for x in l["themes"] if tmap.get(x)}:
                per_llm[responses[i]["llm"]][t] += 1
        def dist(a, b):
            ta, tb = sum(per_llm[a].values()) or 1, sum(per_llm[b].values()) or 1
            return sum(abs(per_llm[a][k] / ta - per_llm[b][k] / tb) for k in ranked_themes) / 2
        for i in range(len(llms)):
            for j in range(i + 1, len(llms)):
                dists.append(dist(llms[i], llms[j]))
        consistency = round((1 - sum(dists) / len(dists)) * 100) if dists else None
    # social validation: theme share in tagged articles by keyword overlap
    def social_share(theme_key: str) -> int | None:
        words = {w for w in re.split(r"[^a-z0-9]+", ttitles.get(theme_key, theme_key).lower()) if len(w) > 3}
        if not words or not articles:
            return None
        hits = 0
        for a in articles:
            text = f"{a.get('theme') or ''} {a.get('summary') or ''}".lower()
            if any(w in text for w in words):
                hits += 1
        return round(hits * 100 / len(articles))
    for t in themes:
        sp = social_share(t["key"])
        if sp is not None:
            t["social_pct"] = sp
        t["llm_pct"] = round(t["count"] * 100 / n_resp) if n_resp else 0

    # ── tab 4: scan ───────────────────────────────────────────────────────
    pillar_rows = []
    heat_rows = []
    for p in pillars:
        stances = [l["pillars"].get(p["key"], "ignore") for l in labs]
        rep = sum(1 for s in stances if s == "reproduce")
        con = sum(1 for s in stances if s == "contradict")
        align = round(rep * 100 / n_resp) if n_resp else 0
        con_share = round(con * 100 / n_resp) if n_resp else 0
        status = "contradicted" if (align < DILUTED_MIN or con_share >= CONTRADICT_SHARE) else ("aligned" if align >= ALIGNED_MIN else "diluted")
        pillar_rows.append({"key": p["key"], "name": p["name"], "intent": p.get("intent", ""), "llm": "", "align": align, "contradict_pct": con_share, "status": status})
        vals = []
        for a in llms:
            sub = [labs[i]["pillars"].get(p["key"], "ignore") for i in range(n_resp) if responses[i]["llm"] == a]
            vals.append(round(sum(1 for s in sub if s == "reproduce") * 100 / len(sub)) if sub else 0)
        heat_rows.append({"pillar": p["name"], "values": vals})
    alignment = round(sum(r["align"] for r in pillar_rows) / len(pillar_rows)) if pillar_rows else None
    counts_status = Counter(r["status"] for r in pillar_rows)
    scores = []
    if strength is not None:
        scores.append({"name": "Narrative strength", "value": strength, "text": ""})
    if consistency is not None:
        scores.append({"name": "Consistency across LLMs", "value": consistency, "text": ""})
    if alignment is not None:
        scores.append({"name": "Positioning alignment", "value": alignment, "text": ""})
    social_gaps = [t for t in themes if t.get("llm_pct", 0) >= SOCIAL_GAP_LLM_MIN and t.get("social_pct") is not None and t["social_pct"] <= SOCIAL_GAP_SOCIAL_MAX]

    interpretation = {
        "banner": {
            "eyebrow": "LLM Interpretation Study · how the brand is described",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{sentiment.get('pos', 0)}%", "label": "Positive"},
                {"value": f"{sentiment.get('neg', 0)}%", "label": "Negative"},
                {"value": f"{sentiment.get('neu', 0)}%", "label": "Neutral"},
                {"value": str(strength) if strength is not None else "—", "label": "Narrative strength · /100"},
            ],
        },
        "note": "",
        "sentiment": {"pos": sentiment.get("pos", 0), "neg": sentiment.get("neg", 0), "neu": sentiment.get("neu", 0)} if sent_rows else None,
        "sentiment_note": "",
        "themes": themes,
        "matrix": {"llms": llms, "rows": matrix_rows, "legend": "Net sentiment per LLM within the theme, -100 to +100."} if matrix_rows else None,
        "language": language,
        "scores": scores,
    }
    scan = {
        "banner": {
            "eyebrow": "Brand Narrative Intelligence Scan · the delta",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(len(pillar_rows)), "label": "Narrative pillars"},
                {"value": str(counts_status.get("aligned", 0)), "label": "Aligned"},
                {"value": str(counts_status.get("diluted", 0)), "label": "Diluted"},
                {"value": str(counts_status.get("contradicted", 0)), "label": "Contradicted"},
            ],
        },
        "note": "",
        "pillars": pillar_rows,
        "heatmap": {"llms": llms, "rows": heat_rows, "legend": "Share of that LLM's responses reproducing the pillar, 0–100."} if heat_rows else None,
        "flags": [],
        "actions": [],
    }
    overview = {
        "banner": {
            "eyebrow": "Congruence & Content Intelligence",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(len(llms)), "label": "LLMs analysed"},
                {"value": str(len(run.get("prompts", []))), "label": "Prompts run"},
                {"value": str(n_resp), "label": "Responses"},
                {"value": f"{total_cites:,}", "label": "Sources cited"},
            ],
        },
        "note": "",
        "stages": STAGES,
        "inputs": INPUTS,
        "datasets": [f"LLM sources ({', '.join(llms) if llms else 'none available'})", "Social validation (tagged posts)", "Brand intent (proposed narrative pillars)" if run.get("pillars_source", "").startswith("proposed") else "Brand intent (positioning statement)"],
        "outcomes": OUTCOMES,
    }
    modes = Counter(r.get("citation_mode", "model-claimed") for r in responses)
    logos = brand_media.brand_logos([brand, *llms], articles)  # the assistants too: the "Cited by" chips
    for s in sources:
        url = brand_media.brandfetch_logo_url(s["domain"])
        if url:
            logos[s["name"]] = url
    # The site each outlet was actually cited from, so logo validation tries
    # that domain first instead of guessing one from the outlet's name.
    logo_domains = {s["name"]: s["domain"] for s in sources if s.get("domain")}
    for outlets in type_outlets.values():  # the source-type icons' outlets, ranked or not
        for name, domain in outlets:
            logo_domains.setdefault(name, domain)
            url = brand_media.brandfetch_logo_url(domain)
            if url:
                logos.setdefault(name, url)

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": run.get("category") or "",
            "competitors": competitors,
            "window": window,
            "llms": llms,
            "prompts_run": len(run.get("prompts", [])),
            "responses": n_resp,
            "sources_cited": total_cites,
            "run_at": run.get("run_at"),
            "citation_mode": dict(modes),
            "assistants_unavailable": run.get("unavailable", {}),
            "pillars_source": run.get("pillars_source"),
            "logos": logos,
            "logo_domains": logo_domains,
            "classification": {"labels": (prepared.get("labels") or {}).get("method"), "themes": theme_meta.get("method"), "errors": {k: len(v) for k, v in (run.get("errors") or {}).items()}},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Congruence & Content Intelligence · AI/LLM Audit and Analysis", f"{len(run.get('prompts', []))} prompts × {len(llms)} LLM{'s' if len(llms) != 1 else ''}, run {run_day}" if llms else "No assistant available for the audit run"],
        "overview": overview,
        "analysis": analysis,
        "interpretation": interpretation,
        "scan": scan,
        "evidence": {
            "responses": [{"llm": r["llm"], "prompt": r["prompt"][:120], "text": r["text"][:500], "sentiment": labels.get(r["id"], {}).get("sentiment"), "sources": [c["outlet"] for c in r.get("citations", [])][:4]} for r in responses[:16]],
            "pillar_examples": {p["key"]: [{"llm": responses[i]["llm"], "text": responses[i]["text"][:300], "stance": labs[i]["pillars"].get(p["key"])} for i in range(n_resp) if labs[i]["pillars"].get(p["key"]) in ("reproduce", "contradict")][:5] for p in pillars},
            "social_gaps": [{"theme": t["name"], "llm_pct": t["llm_pct"], "social_pct": t.get("social_pct")} for t in social_gaps],
        },
    }
