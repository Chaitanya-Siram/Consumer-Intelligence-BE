# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (then one-time browser installs for screenshot / stealth fetchers)
pip install -r requirements.txt
playwright install chromium
scrapling install

# Run dev server (hot-reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run production server (matches Dockerfile CMD)
uvicorn main:app --host 0.0.0.0 --port 8000

# Tests (pytest is not in requirements.txt; install it separately). All offline, ~45s.
python -m pytest tests -q
python -m pytest tests/test_ci_cache_fingerprint.py -q            # one file
python -m pytest tests/test_post_avatar.py -q -k xenforo           # one test by keyword

# Docker build & run
docker build -t pr-solutions-be-v2 .
docker run -p 8000:8000 --env-file .env pr-solutions-be-v2
```

No linter is configured. Tests live in `tests/` as plain `test_*` functions with no conftest or pytest config; they stub network calls and never hit the DB, S3 or an LLM. API docs auto-generated at `/docs` (Swagger) and `/redoc`. README.md is empty.

Two env files exist locally: `.env` (local dev, Docker Postgres replica, `CI_CACHE_SCOPE=local`) and `.env.render` (Render). Never point local at the Render DB unasked.

## Architecture

**FastAPI + PostgreSQL + LLM dual-provider backend** for a PR / consumer intelligence platform.

### Entry Point & Config

- `main.py` — registers 12 routers (11 from `routers/` + `consumer_intelligence.router`), mounts CORS, installs `exception_handlers.validation_exception_handler` (flattens 422 bodies to a single `detail` string), calls `init_db()` at import time
- `configs.py` — `Configs` class loaded as singleton `envs = Configs()` everywhere; also exports the shared `logger`. Core env vars read here via `python-dotenv`; the CI package reads its own optional knobs with `os.getenv` directly

### Layer Structure

```
routers/               → HTTP/WebSocket handlers (one APIRouter per domain) — Media Intelligence pipeline
consumer_intelligence/ → Consumer Intelligence lens backend (own router, builder, storyboards, media, QA) — see below
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
file_helpers/          → S3 (s3_file.py), file parsing (CSV/Excel/DOCX; UTF-16 and non-comma CSV accepted), SimilarWeb reach, publication lookup
reports_helpers/       → Client-specific report generators (beone, otsuka, trane)
tests/                 → Offline pytest suite, almost entirely for consumer_intelligence/ media + cache logic
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
- **CI narratives + classifiers**: `consumer_intelligence/narrative_client.py` — separate async JSON-mode client, same `LLM_PROVIDER` switch; also exposes `get_vision_client()` used by `brand_hero` to confirm a homepage image actually shows the brand
- **Chart agent / section fetcher**: `agents/chart_generator/llm_client.py::complete_json`

### Workflow → Lens Dispatch (MI vs CI)

A session's `workflow` JSON column holds nodes; `analysis` nodes carry `data.lens` and `data.lensType`.

- **Media Intelligence** (`routers/charts_api.py`, `/charts`, `/ws/charts`): collects `data.lens` from analysis nodes → `charts_helpers/` dashboard modules keyed by `DASHBOARDS_ENUM`. After charts compute, `ai_helpers/chart_insight_synthesizer.py` + `storyboard_synthesizer.py` add prose per tab
- **Consumer Intelligence** (`consumer_intelligence/router.py`, `/consumer-intelligence/charts`, `/ws/consumer-intelligence/charts`, `/consumer-intelligence/lenses`): FE calls this when a CI lens is selected. Kept separate so the MI pipeline stays untouched

### Consumer Intelligence Package

Port of the reference `ConsumerIntelligence_PR` backend, adapted to news/social articles (no social engagement → falls back to reach; platform derived from `source name`/domain; signal from subtheme/theme). FE contracts: `Consumer-Intelligence-FE/docs/ci-lens-contract*.md`.

**Core rule: every number is computed in code; the LLM only writes prose and classifies.** `taxonomy.canonicalize`, `narrative.write_narrative`, the per-lens `*_classify.py` prepare passes, `llm_audit`, and the vision check in `brand_hero` are the only LLM entry points. Every LLM/media/network helper **never raises**; on failure the storyboard ships with numbers and empty prose or no media.

#### Registry and build flow

- `tier_registry.py` — the **only place the backend enumerates Tier-1 keys**. `TIER1_TO_LENS_KEYS` (Tier-1 workflow key → lens keys), `COMING_SOON_TIER1` (`influencer_mapping`, `crisis_solutioning` → `{"status": "coming_soon"}`), `CI_LENS_KEYS`, `TIER2_GATE` (lenses that also require their label in the node's `data.tier2[]`; unlisted lenses build whenever their Tier-1 is selected). `resolve_ci_lenses(nodes)` handles `lensType == "tier1"` expansion and standalone CI keys. Mirrors FE `src/workflow/constants.js` — keep in sync
- `builder.py` — `build_ci_charts()` orchestrator. Registries: `_MODULES`, `_ORDER` (cheapest-first), `_BUNDLE` (`brand_intelligence` auto-adds `brand_health_storyboard` + `brand_competitive_intel`), `_NEEDS_TAXONOMY`, `_HAS_PREPARE`, `_HERO_QUERY`, `_VERBATIM_SECTIONS`/`_QUOTE_KEYS`, `_NO_HERO_MEDIA`. Per lens `_build_one`: optional `prepare()` (LLM classify, cached per build by `PREPARE_KEY`; `PREPARE_ACCEPTS_CONTEXT` modules also get `session_id`/`refresh`/`category`) → `build_storyboard()` off-thread (sync, pure) → concurrently `write_narrative()`, hero media, brand assets, verbatim evidence, author/journalist photos → then avatars, product photos, validated logos, leader videos, slot images. After all lenses, `qa_agent.run()` when `with_media`
- `storyboard/<lens>.py` — one module per lens exposing `LENS_KEY` and `build_storyboard(articles, brand=, known_brands=, prepared=...)`. Numbers computed here; **every prose field left empty** for `narrative.py` to fill. Shared metric helpers `brands.py`, `signals.py`, `regional_base.py`
- `storyboard/narrative.py` — `_facts_<lens>()` condenses numbers → one JSON-mode LLM call with strict schema → `_apply_<lens>()` merges defensively; `_REGISTRY` maps lens key → (facts, schema, apply, instruction)
- **Shared substrate for new lenses** (use instead of re-implementing): `timeseries.py` (adaptive week/month buckets, gap-fill, peaks, spikes > 1.6× trailing mean, `halves()`), `cohorts.py` (brand / competitor / industry splits, platform from `source name`, percent rows summing to 100), `quotes.py` (verbatim excerpt picker with platform · host attribution), `taxonomy.py` (one temperature-0 call groups raw `theme` long tail into ≤8 canonical groups with loyalty bucket; annotates `theme_group`; deterministic fallback; stored in `meta.taxonomy`), `aggregate.py` (date parsing, junk-label filtering)

#### Lenses shipped

| Tier-1 key | Lens keys (module) | Notes |
|---|---|---|
| `brand_intelligence` | `brand_intelligence` (+bundled `brand_health_storyboard`, `brand_competitive_intel`), `brand_perception` | `brand_perception` gated; popularity reuses `brands.mention_counts` |
| `market_intelligence` / `network_map_analysis` / standalone `trend_intelligence` | `market_intelligence`, `network_map`, `trend_intelligence` | original port |
| `issues_intelligence` / `advanced_metrics` | `track_emerging_issues`, `shifting_audience_priorities` | taxonomy-driven; loyalty `WEIGHTS` placeholder |
| `landscape_analysis` | `perception_analysis`, `dominant_narratives` | gated; six fixed perception keys, batched per-post labels |
| `whitespace_gap_analysis` | `audience_expectation`, `brand_messaging`, `brand_performance` | gated; share `whitespace_classify.py` via `PREPARE_KEY="whitespace"` |
| `consumer_segmentation` | `user_behaviour` | gated; life-stage inference at confidence ≥ 0.6 |
| `regional_intelligence` | `regional_sentiment`, `regional_engagement`, `regional_brand_perception` | gated; share `regional_classify.py` (`PREPARE_KEY="regional"`) + `regional_base.compute_regions`; tagger `region` field is the configured market, reported under `sources.default`, not evidence |
| `llm_audit` | `congruence_content` | gated; primary input is `llm_audit.py` run (ChatGPT/Claude/Perplexity/Gemini adapters, cached on S3 `.../llm_audit/latest.json` for `LLM_AUDIT_TTL_DAYS`, `citation_mode` grounded vs model-claimed); `PREPARE_ACCEPTS_CONTEXT` |
| `social_research`, `social_listening`, `social_audit`, `pr_research` | same key each | **one lens, sub-lenses are `TABS` inside it**, not separate keys. Taxonomy-driven; social_listening/social_audit are single-brand (no competitor set); pr_research splits early/late via `timeseries.halves()` and resolves author headshots via Muck Rack (scrapling). Carry "Supporting Verbatims" sections listed in `_VERBATIM_SECTIONS` |

#### Media, evidence and QA (all under `with_media`, all never raise)

- `brand_media.py` — Pexels photos/videos, Brandfetch, DuckDuckGo image search (`ddgs`, keyless, tried **before** Pexels via `stock_photo`), URL liveness checks, `resolve_hero_media`, `resolve_brand_assets`, `resolve_slot_images` (per-dimension/tab sub-banners), Muck Rack author photos, Iconify flags
- `brand_hero.py` — brand's own homepage first for the hero: embedded video → og:image verified by a vision call → else Pexels. Cached per domain for process life. `brand_video.py` — official YouTube channel (linked from the brand site, never searched) RSS feed matched to tab label, for Brand Intelligence leader cards
- `logo_resolver.py` — validated `meta.logos` registry: Brandfetch → Google favicon → Iconify, rejects placeholders/duplicate bytes, editorial names get no logo
- `verbatim_capture.py` — per quote: verified X/YouTube oEmbed embed → Playwright screenshot cached on S3 `verbatim_screenshots/` → scraped preview card → plain text. Served via `/consumer-intelligence/verbatim-image?key=`
- `profile_images.py` + `post_avatar.py` — poster avatars (unavatar for X/YouTube, Tumblr API, forum/review pages scraped with scrapling; Instagram/Facebook/Reddit only with `UNAVATAR_API_KEY`), stored on S3 `profile_images/`, served via `/consumer-intelligence/profile-image?key=`. `product_images.py` — Pexels stock photo per Brand Perception product card, labelled stock
- `qa_agent.py` — post-build verify-and-repair over the whole payload (post evidence ladder, quote text must be in its article, links http(s), every image/banner/logo/avatar actually loads; re-resolves or clears). Runs `CI_QA_ITERATIONS` passes (default 3, min 2) within `CI_QA_TIME_BUDGET_SECONDS`; writes `meta.qa`. Also callable via `POST /consumer-intelligence/qa`. `qa_render.py` — opens the FE in headless Chromium with the caller's bearer token to confirm images render (`POST /consumer-intelligence/qa/render`, needs `FRONTEND_URL`)
- Extra endpoints: `GET /consumer-intelligence/brand-hero?brand=&domain=`, `GET /consumer-intelligence/stock-image?query=` (lens-picker cards)

#### Caching (incremental, fingerprinted)

Payload on S3 at `session_files/[{CI_CACHE_SCOPE}/]session_{id}/ci_charts_data/latest.json` (+ timestamped copy). `router._plan` compares cached lens keys with required keys and rebuilds only missing ones, merging via `_merge_payload` (drops stale coming-soon stubs). `refresh=true` rebuilds requested lenses but keeps others, so `?lenses=x&refresh=true` refreshes one lens safely. Every payload carries `meta.source`, a fingerprint of the session record (created_at, brand/competitor keywords) plus article ids and approved count; a mismatched fingerprint discards the cache (session ids get reused across recreated sessions / restored DBs). **Saved after every lens**, not only at the end: `build_ci_charts(on_lens_built=)` hands the router a snapshot (`meta.partial: true`) after each lens and `router._persist` merges, stamps and writes `latest.json`; the final write also adds the timestamped copy. A process killed mid-build (Render's 512 MiB free tier OOM-kills long builds) therefore leaves finished lenses for the next request, which builds only the rest. Concurrent GETs for the same session and lens set share one in-flight build (`router._inflight`). Both the GET and WS handlers copy the session fields they need via `_build_inputs` and call `db.close()` before the build, so waiting requests hold no pooled connection (pool is 5 + 10 overflow; idle holders produced `QueuePool limit reached` 500s). Local dev shares the S3 bucket with Render but not the DB, hence `CI_CACHE_SCOPE=local`.

#### Adding a lens

Tier-2 under an existing pillar: builder module with `LENS_KEY` + `build_storyboard()` (+ optional `prepare`, `PREPARE_KEY`) → register in `builder._MODULES/_ORDER/_HERO_QUERY` (+ `_HAS_PREPARE`, `_NEEDS_TAXONOMY`, `_NO_HERO_MEDIA`, `_VERBATIM_SECTIONS`) → facts/schema/apply + `_REGISTRY` entry in `narrative.py` → `tier_registry.TIER1_TO_LENS_KEYS`, `CI_LENS_KEYS` (+ `TIER2_GATE`) → FE `CI_LENS_KEYS`/`TIER1_TO_CI_KEYS`, screen, route, `tierLensData` `route:`. New Tier-1 pillar: one `TIER1_TO_LENS_KEYS` line plus the lens. Add an offline test in `tests/` for any media resolver.

- Article gate matches MI: `is_approved_for_dashboards` rows if any approved, else all `is_relevant` rows
- WS message types: `start` (with `cached` list), `progress` (stages `taxonomy`/`classify`/`storyboard`/`narrative`), `lens_complete` (carries finished storyboard), `lens_error`, `complete` (with `incremental` or `cached` flag), `error`

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

GitHub Actions (`.github/workflows/pr-solutions-dev.yaml`): push to `main` → Docker build → AWS ECR (`958065208983.dkr.ecr.us-west-1.amazonaws.com/pr-solutions-v2:<run_number>`) → replaces `_BUILD__ID_` in `build/pr-solutions-dev.yaml` → `kubectl apply` to EKS cluster `AMX-EKS`, namespace `pr-solutions-v2` → MS Teams notification. New env vars must be added to the k8s manifest's secret refs to reach the deployed pod; the manifest currently lacks every CI-package variable below (Brandfetch, Pexels, LLM audit, QA, `FRONTEND_URL`, `CI_CACHE_SCOPE`), so the k8s pod runs the CI lenses media-less. The Dockerfile does not run `playwright install` or `scrapling install`, so screenshots and stealth fetches are unavailable in that image too. Render is the other deploy target and does not auto-deploy on push.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `DB_HOST/PORT/NAME/USER/PASSWORD`, `DB_SCHEMA` | PostgreSQL connection |
| `LLM_PROVIDER` | `gpt` (default) or `claude`; see aliases above |
| `LLM_CONCURRENCY`, `LLM_BATCH_SIZE` | Tagging parallelism / batch size |
| `RELEVANCY_MIN_CONFIDENCE` | Relevancy agent gate (default 0.5) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `MAX_OUTPUT_TOKENS` | Anthropic Claude |
| `AZURE_OPENAI_API_KEY/ENDPOINT/MODEL/API_VERSION`, `AZURE_OPENAI_WEB_SEARCH_MODEL` | Azure OpenAI |
| `AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `AWS_S3_BUCKET` | S3 storage (session files, CI cache, screenshots, avatars) |
| `AWS_S3_REACH_BUCKET/REACH_FILE`, `SIMILAR_WEB_REST_API_KEY`, `PUBLICATION_SOURCE_FILE` | Reach / publication lookups |
| `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | Auth |
| `CREDENTIALS_ENCRYPTION_KEY` | AES for stored provider credentials |
| `EMBED_PROVIDER/MODEL/DIM`, `NVIDIA_EMBED_API_KEY/URL/MODEL/BATCH_SIZE` | RAG embeddings (`EMBED_DIM` must match pgvector column: nemotron 2048, BGE 1024) |
| `RERANK_ENABLED/PROVIDER/MODEL`, `CHUNK_SIZE/OVERLAP`, `RETRIEVE_TOP_K`, `RERANK_TOP_N`, `VECTOR_TABLE_NAME` | RAG tuning |
| `E2B_API_KEY` | Sandboxed code execution for chart agent |
| `SERP_API_KEY` | SerpAPI Google News |
| `BRANDFETCH_CLIENT_ID`, `BRANDFETCH_API_KEY`, `PEXELS_API_KEY`, `UNAVATAR_API_KEY` | CI storyboard media (all optional; unavatar paid key unlocks Instagram/Facebook/Reddit avatars) |
| `CI_CACHE_SCOPE` | Optional S3 key namespace for the CI charts cache; `local` in dev `.env`, empty on Render |
| `DDGS_IMAGE_BACKEND`, `DDGS_TEXT_BACKEND`, `DDGS_TIMEOUT` | `ddgs` metasearch engines pinned to ones that answer from the server IP (defaults `bing` / `yahoo,startpage`, 4s); DuckDuckGo itself times out from Render |
| `CI_QA_ITERATIONS`, `CI_QA_TIME_BUDGET_SECONDS` | QA agent passes (default 3, min 2) and wall-clock budget |
| `FRONTEND_URL` | FE origin for `qa_render` browser check and logo-fetch Referer (default `http://localhost:3000`) |
| `LLM_AUDIT_ASSISTANTS`, `LLM_AUDIT_TTL_DAYS`, `LLM_AUDIT_CONCURRENCY` | LLM audit run: assistants to attempt (default ChatGPT,Claude,Perplexity,Gemini), run reuse window (7), parallel calls (4) |
| `PERPLEXITY_API_KEY`, `PERPLEXITY_MODEL`, `GEMINI_API_KEY`, `GEMINI_MODEL` | Optional extra audit assistants; unset means the assistant is reported unavailable, never simulated |
| `LOG_LEVEL`, `ENVIRONMENT` | Logging level; deployment label |
