# Architecture Review: open-webui-custom + deepagent-project

## Executive Summary

The current two-repo setup has a fundamental architectural mismatch. `open-webui-custom` is described as "frontend only" but ships an entire Python backend (70MB, 221 Python files, 160 pip dependencies including PyTorch, whisper, sentence-transformers). Meanwhile, `deepagent-project` contains its own separate frontend (`deep-agents-ui/`, Next.js/React) alongside its LangGraph agent backend. The result: **two frontends and two backends across two repos**, when the intent is one frontend + one backend.

---

## Current State Analysis

### open-webui-custom (This Repo)

**What it is:** A fork of the official Open WebUI monolith (v0.8.10)

| Metric | Value |
|---|---|
| Svelte components | 549 |
| Python backend files | 221 |
| Backend size | 70 MB |
| Frontend size | 13 MB |
| Python dependencies | 160 |
| Node dependencies | 90+ |
| Docker image (estimated) | 2-4 GB (includes torch, whisper, embedding models) |

**What you actually need from it:** The Svelte chat UI (~13MB).

**What you're shipping but don't use:**
- Full FastAPI backend with 27 API routers
- SQLAlchemy + Alembic + Peewee (3 ORMs)
- Ollama integration, RAG pipeline, whisper/TTS
- PyTorch, sentence-transformers, embedding models
- Image generation, audio processing
- LDAP, OAuth, SCIM authentication
- 9 vector database integrations

### deepagent-project

**What it is:** A LangGraph-based agent service with an embedded Next.js frontend.

| Metric | Value |
|---|---|
| Agent tools | 3 (internet_search, visit_webpage, current_server_time) |
| Model | DeepSeek Reasoner (custom wrapper) |
| Frontend framework | Next.js 15 / React 19 |
| Git commits | 2 |
| Production readiness | Low |

**Issues found:**
- Duplicated file: `deepseek_reasoner_chat_model.py` exists at root AND in package
- Dead files: `main.py` (empty), `1.deep_agent_research.py`, `2.deep_agents_info.py`
- Root `pyproject.toml` is empty/unused
- No structured logging (print statements only)
- No health checks in Docker
- Port mismatch: Dockerfile exposes 32000, docker-compose maps 2024:2024
- Uses `langgraph dev` (development mode) in production Dockerfile
- No tests beyond a single integration script

---

## Key Problems

### 1. Massive Dead Weight
The Dockerfile downloads PyTorch, sentence-transformers, and whisper models during build. For a "frontend only" use case, this wastes 2-4GB of Docker image.

### 2. Two Frontends, Neither Integrated
- **open-webui-custom** (Svelte): Coupled to Open WebUI's Python backend APIs
- **deep-agents-ui** (React/Next.js): Coupled to LangGraph SDK APIs
- Different frameworks, state management, API clients, component libraries

### 3. No Unified Deployment
Two independent docker-compose files. No single `docker compose up` for the whole app. Port conflicts (both try to use 3000).

### 4. No Shared API Contract
No OpenAPI spec or shared types between frontend and backend.

---

## Recommended Reorganization

### Option A: Monorepo (Recommended)

```
deepagent-app/
├── docker-compose.yml          # Single orchestration file
├── .env.example                # Unified config
│
├── frontend/                   # ONE frontend (Svelte or React)
│   ├── Dockerfile              # node + static build only (~100MB image)
│   ├── nginx.conf              # Serve static + proxy /api to backend
│   ├── package.json
│   └── src/
│
├── backend/                    # LangGraph agent service
│   ├── Dockerfile              # Python slim, no torch/whisper (~200MB image)
│   ├── pyproject.toml          # Single dependency definition
│   ├── src/
│   │   ├── agents/             # Agent definitions (pluggable)
│   │   ├── tools/              # Tool implementations
│   │   ├── models/             # Custom model wrappers (ONE copy)
│   │   ├── config.py           # pydantic-settings
│   │   └── logging.py          # Structured logging
│   └── tests/
│
└── shared/                     # API contracts
    └── api-schema.json
```

### Option B: Two Repos, Clean Separation

1. **Strip open-webui-custom**: Delete `backend/`, use nginx to serve static SPA + proxy to deepagent
2. **Clean deepagent-project**: Remove `deep-agents-ui/`, delete dead files, use production server

---

## Priority Action Items

### Immediate
1. Decide on ONE frontend (Svelte or React)
2. Decouple open-webui backend — build as static SPA, proxy API to deepagent
3. Delete dead code in deepagent-project
4. Fix port mapping (expose 32000 matches container, or remap correctly)

### Short-term
5. Unified docker-compose.yml for full stack
6. Health checks on both services
7. Replace print() with structured logging
8. Switch from `langgraph dev` to production server
9. Define API contract

### Medium-term
10. Plugin-style agent architecture for extensibility
11. Proper test suite (unit + integration)
12. CI/CD pipeline
13. Configuration validation (pydantic-settings / zod)

---

## Frontend Decision Matrix

| Factor | Open WebUI (Svelte) | deep-agents-ui (React) |
|---|---|---|
| Maturity | Very mature, 549 components | Minimal, ~10 components |
| Features | Chat, RAG, admin, i18n, tools | Basic chat only |
| Bundle size | Heavy | Light |
| Backend coupling | Tight to its Python backend | Designed for LangGraph |
| Effort to adapt | Strip down from full app | Build up from minimal |
