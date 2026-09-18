# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run dev server (hot-reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run production server (matches Dockerfile CMD)
uvicorn main:app --host 0.0.0.0 --port 8000

# Docker build & run
docker build -t pr-solutions-be-v2 .
docker run -p 8000:8000 --env-file .env pr-solutions-be-v2
```

No test runner or linter is configured. API docs auto-generated at `/docs` (Swagger) and `/redoc`. README.md is empty.

## Architecture

**FastAPI + PostgreSQL + LLM dual-provider backend** for a PR / consumer intelligence platform.

### Entry Point & Config

- `main.py` — registers 12 routers (11 from `routers/` + `consumer_intelligence.router`), mounts CORS, calls `init_db()` at import time
- `configs.py` — `Configs` class loaded as singleton `envs = Configs()` everywhere; also exports the shared `logger`. All env vars read here via `python-dotenv`

### Layer Structure

```
routers/               → HTTP/WebSocket handlers (one APIRouter per domain) — Media Intelligence pipeline
consumer_intelligence/ → Consumer Intelligence lens backend (own router, builder, storyboards) — see below
db_helpers/
  models/              → SQLAlchemy ORM classes + Pydantic response schemas, co-located per entity
  repository/          → Plain functions wrapping SQLAlchemy queries per domain
  repository/auth_repository/ → JWT/bcrypt (security.py), refresh tokens, AES crypto, FastAPI Depends guards (dependencies.py)
  database.py          → Engine, SessionLocal, get_db(), init_db()
  schema.py            → DASHBOARDS_ENUM and other shared enums
  workflow_validator.py→ validates session workflow JSON (nodes/edges)
ai_helpers/            → Tagging LLM providers (claude_service / openai_service), factory llm_service.py,
                         synthesizers (narrative, chart insights, storyboard chapters), prompts/
agents/                → chart_generator (E2B sandbox), relevancy_agent, workflow_agent (LangGraph), section_fetcher
rag_helpers/           → LlamaIndex + pgvector RAG (ingestion, retrieval, reranker, LangGraph agent); used by agent_api & tagging_api
charts_helpers/        → MI chart computation (dashboards, media_monitoring, pr_calculation, reputation, default_charts)
data_source_helpers/   → Article fetchers (Google News RSS, SerpAPI, Tavily, Phyllo) — fetching_service_v2.py, api_sources/
file_helpers/          → S3 (s3_file.py), file parsing (CSV/Excel/DOCX), SimilarWeb reach, publication lookup
reports_helpers/       → Client-specific report generators (beone, otsuka, trane)
build/                 → k8s Deployment+Service manifest applied by CI (secret: pr-solution-v2-secret)
```

### Database

- PostgreSQL, schema from `DB_SCHEMA` (default `pr_solution`); `Base` uses `MetaData(schema=DB_SCHEMA)` so string FKs resolve within it; `search_path` set to `{DB_SCHEMA},public`
- SQLAlchemy 2.0 sync ORM; **no migration tool (no Alembic)** — `init_db()` runs `Base.metadata.create_all()` at startup. Adding a column to an existing table requires manual DDL
- Engine uses a NaN/Inf-safe JSON serializer for all JSONB columns (uploaded CSVs produce NaN)
- pgvector extension enabled on startup (guarded; warns if privilege missing). RAG chunks live in `data_rag_chunks` (LlamaIndex-managed, name from `VECTOR_TABLE_NAME` prefixed with `data_`)
- Tagged articles store sentiment as `POS`/`NEG`/`NEU` codes; `subtheme`/`theme`/`section` fields drive both MI and CI charts

### Auth Flow

- OAuth2 password flow → JWT access tokens (HS256) + rotating refresh tokens stored SHA-256 hashed in DB
- Tokens carry `org_id`, `role`, `mapping_id` claims → org-scoped authorization
- Guards in `db_helpers/repository/auth_repository/dependencies.py`: `get_current_user`, `get_org_id`, `require_org_admin`, `require_superadmin`, `require_superadmin_or_secret` (also accepts `X-Admin-Secret` header)
- Data-provider credentials encrypted at rest (AES via `CREDENTIALS_ENCRYPTION_KEY`, falls back to `JWT_SECRET_KEY`)

### LLM Provider Switching

`LLM_PROVIDER` env var (default `gpt`) picks the provider. Aliases: `claude`/`anthropic` → Anthropic; `gpt`/`openai`/`azure`/`azure_openai` → Azure OpenAI.

- **Article tagging**: `ai_helpers/llm_service.py` dispatches `tag_articles` / `tag_articles_streaming` to `claude_service.py` or `openai_service.py` (same signatures)
- **CI narratives**: `consumer_intelligence/narrative_client.py` — separate async JSON-mode client, same `LLM_PROVIDER` switch
- **Chart agent / section fetcher**: `agents/chart_generator/llm_client.py::complete_json`

### Workflow → Lens Dispatch (MI vs CI)

A session's `workflow` JSON column holds nodes; `analysis` nodes carry `data.lens` and `data.lensType`.

- **Media Intelligence** (`routers/charts_api.py`, `/charts`, `/ws/charts`): collects `data.lens` from analysis nodes → `charts_helpers/` dashboard modules keyed by `DASHBOARDS_ENUM`. After charts compute, `ai_helpers/chart_insight_synthesizer.py` + `storyboard_synthesizer.py` add prose per tab
- **Consumer Intelligence** (`consumer_intelligence/router.py`, `/consumer-intelligence/charts`, `/ws/consumer-intelligence/charts`, `/consumer-intelligence/lenses`): FE calls this when a CI lens is selected. Kept separate so the MI pipeline stays untouched

### Consumer Intelligence Package

Port of the reference `ConsumerIntelligence_PR` backend, adapted to news articles (no social engagement → falls back to reach; platform derived from section/domain; signal from subtheme/theme).

- `tier_registry.py` — `TIER1_TO_LENS_KEYS` (Tier 1 workflow key → CI lens keys), `COMING_SOON_TIER1` (returns `{"status": "coming_soon"}`), `resolve_ci_lenses(nodes)` handles `lensType == "tier1"` expansion and standalone CI lens keys. Mirrors FE `src/workflow/constants.js` — keep in sync
- `builder.py` — `build_ci_charts()` orchestrator. Per lens: `build_storyboard()` off-thread → `write_narrative()` and hero media/brand assets run concurrently. Build order is cheapest-first. `brand_intelligence` auto-bundles `brand_health_storyboard` + `brand_competitive_intel`. Emits events via `on_event` for WS streaming
- `storyboard/<lens>.py` — one module per lens, each exposes `LENS_KEY` and `build_storyboard(articles, brand=, known_brands=)`. **Numbers computed here; every prose field left empty** for `narrative.py` to fill. Lenses: trend, brand_intel, health, bci, market_intel, network_map. Shared metric helpers: `brands.py`, `signals.py`
- `storyboard/narrative.py` — `_facts_<lens>()` condenses numbers → single JSON-mode LLM call with strict schema → `_apply_<lens>()` merges defensively. **Never raises**; on failure the storyboard ships with numbers and empty prose
- `aggregate.py` — pure aggregation helpers over tagged articles (date parsing, junk-label filtering)
- `brand_media.py` — Brandfetch CDN/API logos, Pexels hero images, Iconify flag codes. All async, never raise, return `None` on failure
- **Shared substrate for new lenses** (use these instead of re-implementing per lens): `timeseries.py` (adaptive week/month buckets — weekly under 90 days, gap-filled series, peaks, spikes > 1.6× trailing mean, early/late halves, period pairs), `cohorts.py` (brand / top-competitor / industry splits, single-vs-multiple brand, platform from `source name`, percent rows that sum to exactly 100), `quotes.py` (verbatim excerpt picker with platform · host attribution), `taxonomy.py` (one temperature-0 LLM call groups the raw `theme` long tail into ≤8 canonical groups with a loyalty bucket each; annotates `theme_group`; deterministic top-N fallback; stored in `meta.taxonomy`). Lenses in `builder._NEEDS_TAXONOMY` get annotated articles
- **LLM boundary**: prose and *classification* (theme grouping, issue naming, spike labels) may come from the LLM; every number is computed in code. `taxonomy.canonicalize` and `narrative.write_narrative` are the only LLM entry points
- Tier-2 lenses shipped: `track_emerging_issues` (`storyboard/emerging_issues.py`, issue = theme group ranked by growth + negativity + unmet needs + brand share), `shifting_audience_priorities` (`storyboard/audience_priorities.py`, loyalty index 10–100 from weighted bucket shares; `WEIGHTS` is a placeholder until deck values arrive), `perception_analysis` (`storyboard/perception.py` + `perception_classify.py`: raw themes → six fixed perception keys, negatives → fear/anger + aspect; no emotion tag exists so mix derives from sentiment), `dominant_narratives` (`storyboard/dominant_narratives.py` + `narratives_classify.py`: one batched per-post pass labels usage group / question intent / outlook attitude, a merge call folds outlook labels into 6–9 themes; platforms and brand shares reuse `cohorts`; no secondary-research input so `ext: true` is never emitted), `brand_perception` (`storyboard/brand_perception.py` + `brand_perception_classify.py`: popularity reuses `brands.mention_counts` ÷ brand-tagged posts, so it shares the tally with Brand & Competitive but not its denominator; batched per-post pass extracts brand-qualified product names, switching intent and one of five driver keys; gated on "Brand Perception" under `brand_intelligence`), and the three Whitespace & Gap lenses `audience_expectation` / `brand_messaging` / `brand_performance` (`storyboard/audience_expectation.py`, `brand_messaging.py`, `brand_performance.py` sharing `whitespace_classify.py`: one batched per-post pass labels attribute, unmet need, digital complaint, digital topic, brand initiative and usage; modules declare `PREPARE_KEY = "whitespace"` so the builder's per-build prepare cache runs it once; no brand-owned flag and no secondary research, so initiatives come from LLM classification and `mobile`/`survey`/`ext` are never emitted), and `user_behaviour` under the new Tier-1 `consumer_segmentation` (`storyboard/user_behaviour.py` + `behaviour_classify.py`: LLM life-stage inference per post gated at confidence 0.6, segment shares only when ≥20% of posts are banded; `brand_choice` from `brands.mention_counts` over multi-product posts; question bullets only for reason/rule clusters with ≥3 posts). `tier_registry.py` is the only place the backend enumerates Tier-1 keys, so a new pillar is one mapping line plus a gate. Contracts: `Consumer-Intelligence-FE/docs/ci-lens-contract*.md`
- **Tier-2 gating**: by default every lens under a selected Tier-1 is built (Brand Intelligence parity). `tier_registry.TIER2_GATE` lists lenses that also require their label in the node's `data.tier2[]` (Perception Analysis does)
- **Per-lens LLM prep**: modules in `builder._HAS_PREPARE` expose `async prepare(articles, brand=, known_brands=)`; its result is passed to `build_storyboard(prepared=...)`. Use this for classification a lens needs before counting; keep `build_storyboard` sync and pure
- **Caching is incremental**: payload stored on S3 at `session_files/[{CI_CACHE_SCOPE}/]session_{id}/ci_charts_data/latest.json` (+ timestamped copy). `router._plan` compares cached lens keys with the session's required keys and rebuilds only the missing ones, merging via `_merge_payload` (drops stale coming-soon stubs). `refresh=true` rebuilds the requested lenses but keeps other cached ones, so `?lenses=x&refresh=true` refreshes one lens safely. Local dev shares the S3 bucket with Render but not the DB, so set `CI_CACHE_SCOPE=local` in the dev `.env` (Render leaves it empty)
- Adding a Tier-2 lens: builder module with `LENS_KEY` + `build_storyboard()` (+ optional `prepare`) → register in `builder._MODULES/_ORDER/_HERO_QUERY` (+ `_HAS_PREPARE`, `_NO_HERO_MEDIA`) → facts/schema/apply + `_REGISTRY` entry in `narrative.py` → `tier_registry.TIER1_TO_LENS_KEYS`, `CI_LENS_KEYS` (+ `TIER2_GATE` if only some Tier-2 selections should build it) → FE `CI_LENS_KEYS`/`TIER1_TO_CI_KEYS`, screen, route, `tierLensData` `route:`
- Article gate matches MI: `is_approved_for_dashboards` rows if any approved, else all `is_relevant` rows
- WS message types: `start` (with `cached` list), `progress` (stages `taxonomy`/`storyboard`/`narrative`), `lens_complete` (carries finished storyboard), `lens_error`, `complete` (with `incremental` flag), `error`

### WebSocket Endpoints

- `/ws/tagging` — streaming article tagging (per-batch progress)
- `/ws/charts` — MI chart generation
- `/ws/consumer-intelligence/charts` — CI storyboard streaming (init message: `{"session_id": int, "lenses": [..]?, "refresh": bool?, "with_media": bool?}`)
- `/ws/agent` — AI chart agent (E2B sandboxed code execution)
- `/ws/workflow-agent` — conversational workflow builder (LangGraph, 40-turn limit, 4000 char/message); state in-memory per connection
- Blocking LLM/CPU calls offloaded via `asyncio.to_thread()`

### Data Pipeline

Raw articles → relevancy agent gate → tagged articles → MI charts / CI storyboards / reports

Sources (Google News RSS, SerpAPI, Tavily, Phyllo) → `raw_articles` → `agents/relevancy_agent/` (threshold `RELEVANCY_MIN_CONFIDENCE`) → `tagged_articles`. Workflow changes trigger retagging of articles inserted in the last 24h.

### CI/CD

GitHub Actions (`.github/workflows/pr-solutions-dev.yaml`): push to `main` → Docker build → AWS ECR (`958065208983.dkr.ecr.us-west-1.amazonaws.com/pr-solutions-v2:<run_number>`) → replaces `_BUILD__ID_` in `build/pr-solutions-dev.yaml` → `kubectl apply` to EKS cluster `AMX-EKS`, namespace `pr-solutions-v2` → MS Teams notification. New env vars must be added to the k8s manifest's secret refs to reach the deployed pod.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `DB_HOST/PORT/NAME/USER/PASSWORD`, `DB_SCHEMA` | PostgreSQL connection |
| `LLM_PROVIDER` | `gpt` (default) or `claude`; see aliases above |
| `LLM_CONCURRENCY`, `LLM_BATCH_SIZE` | Tagging parallelism / batch size |
| `RELEVANCY_MIN_CONFIDENCE` | Relevancy agent gate (default 0.5) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `MAX_OUTPUT_TOKENS` | Anthropic Claude |
| `AZURE_OPENAI_API_KEY/ENDPOINT/MODEL/API_VERSION`, `AZURE_OPENAI_WEB_SEARCH_MODEL` | Azure OpenAI |
| `AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `AWS_S3_BUCKET` | S3 storage (session files, CI cache) |
| `AWS_S3_REACH_BUCKET/REACH_FILE`, `SIMILAR_WEB_REST_API_KEY`, `PUBLICATION_SOURCE_FILE` | Reach / publication lookups |
| `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | Auth |
| `CREDENTIALS_ENCRYPTION_KEY` | AES for stored provider credentials |
| `EMBED_PROVIDER/MODEL/DIM`, `NVIDIA_EMBED_API_KEY/URL/MODEL` | RAG embeddings (`EMBED_DIM` must match pgvector column: nemotron 2048, BGE 1024) |
| `RERANK_ENABLED/PROVIDER/MODEL`, `CHUNK_SIZE/OVERLAP`, `RETRIEVE_TOP_K`, `RERANK_TOP_N`, `VECTOR_TABLE_NAME` | RAG tuning |
| `E2B_API_KEY` | Sandboxed code execution for chart agent |
| `SERP_API_KEY` | SerpAPI Google News |
| `BRANDFETCH_CLIENT_ID`, `BRANDFETCH_API_KEY`, `PEXELS_API_KEY` | CI storyboard media (all optional) |
| `CI_CACHE_SCOPE` | Optional S3 key namespace for the CI charts cache; `local` in dev `.env`, empty on Render |
