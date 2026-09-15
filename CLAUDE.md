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

No test runner or linter is configured. API docs auto-generated at `/docs` (Swagger) and `/redoc`.

## Architecture

**FastAPI + PostgreSQL + LLM dual-provider backend** for a PR intelligence platform.

### Entry Point & Config

- `main.py` — registers all 11 routers, mounts CORS middleware, calls `init_db()` on startup
- `configs.py` — `Configs` class loaded as singleton `envs = Configs()` throughout codebase; reads all env vars via `python-dotenv`

### Layer Structure

```
routers/          → HTTP/WebSocket route handlers (FastAPI APIRouter per domain)
db_helpers/
  models/         → SQLAlchemy ORM classes + Pydantic response schemas, co-located per entity
  repository/     → Plain functions wrapping SQLAlchemy queries per domain
  database.py     → Engine creation, SessionLocal, get_db() dependency
  auth_repository/→ JWT/bcrypt security, refresh tokens, org-scoped claims, FastAPI Depends guards
ai_helpers/       → LLM wrappers (Claude & Azure OpenAI), synthesizers (narrative, chart insights)
agents/           → Chart generator agent, relevancy scoring agent, workflow builder agent
rag_helpers/      → LlamaIndex + pgvector RAG pipeline (ingestion, retrieval, LangGraph agent)
charts_helpers/   → Chart computation modules (dashboards, media monitoring, PR calc, reputation)
data_source_helpers/ → External data fetchers (Google News RSS, SerpAPI, Tavily, Phyllo)
file_helpers/     → S3 upload/download, file parsing (CSV/Excel/DOCX), SimilarWeb
reports_helpers/  → Client-specific report generators
```

### Database

- PostgreSQL with schema `pr_solution` (env: `DB_SCHEMA`); pgvector extension enabled on startup
- SQLAlchemy 2.0 sync ORM; **no migration tool (no Alembic)** — `init_db()` calls `Base.metadata.create_all()` at startup
- Schema changes require manual DDL or table recreation
- RAG vector store uses a separate `data_rag_chunks` table managed by LlamaIndex

### Auth Flow

- OAuth2 password flow → JWT access tokens (HS256) + rotating refresh tokens stored hashed (SHA-256) in DB
- Tokens carry `org_id`, `role`, `mapping_id` claims → org-scoped authorization
- FastAPI `Depends` guards in `db_helpers/auth_repository/dependencies.py`:
  - `get_current_user`, `get_org_id`, `require_org_admin`, `require_superadmin`
  - `require_superadmin_or_secret` — also accepts `X-Admin-Secret` header
- Data-provider credentials encrypted at rest (AES via `CREDENTIALS_ENCRYPTION_KEY`)

### LLM Provider Switching

`LLM_PROVIDER` env var switches between `"claude"` (Anthropic) and `"gpt"` (Azure OpenAI). Both providers implement the same interface — `ai_helpers/claude_service.py` and `ai_helpers/openai_service.py`. All routers import a shared factory that returns the active provider.

### WebSocket Endpoints

- `/ws/agent` — AI chart agent (streaming chart generation via E2B sandboxed code execution)
- `/ws/workflow-agent` — multi-turn conversational workflow builder (LangGraph state machine, 40-turn limit, 4000 char/message limit); state is in-memory per connection
- Blocking LLM calls offloaded via `asyncio.to_thread()`

### Data Pipeline

Raw articles → relevancy agent gate → tagged articles → charts/reports

Sources: Google News RSS, SerpAPI, Tavily, Phyllo → `raw_articles` table → relevancy scoring (`agents/relevancy_agent/`) → `tagged_articles` table

### CI/CD

GitHub Actions (`.github/workflows/pr-solutions-dev.yaml`): push to `main` → Docker build → AWS ECR (`958065208983.dkr.ecr.us-west-1.amazonaws.com/pr-solutions-v2`) → `kubectl apply` to EKS cluster `AMX-EKS` namespace `pr-solutions-v2` → MS Teams notification.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `DB_HOST/PORT/NAME/USER/PASSWORD/DB_SCHEMA` | PostgreSQL connection |
| `LLM_PROVIDER` | `"gpt"` or `"claude"` |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | Anthropic Claude |
| `AZURE_OPENAI_API_KEY/ENDPOINT/MODEL` | Azure OpenAI |
| `AWS_ACCESS_KEY_ID/SECRET/REGION/S3_BUCKET` | S3 storage |
| `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | Auth |
| `CREDENTIALS_ENCRYPTION_KEY` | AES encryption for stored provider credentials |
| `NVIDIA_EMBED_API_KEY/URL/MODEL` | Embeddings for RAG |
| `E2B_API_KEY` | Sandboxed code execution for chart agent |
| `SERP_API_KEY` | SerpAPI Google News |
