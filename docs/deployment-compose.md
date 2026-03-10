# Deployment Compose Layout

This repository now uses a base + override compose structure for clearer long-term maintenance.

## Files

- `docker-compose.yaml`: base stack (`ollama`, `open-webui`)
- `docker-compose.openai-adapter.yaml`: adds `openai-adapter` and rewires OpenWebUI OpenAI URL to adapter

## Environment Variable Scope

| Variable | Base stack (`docker-compose.yaml`) | Adapter override (`docker-compose.openai-adapter.yaml`) |
| --- | --- | --- |
| `OPEN_WEBUI_PORT` | Used | Used (inherited) |
| `OLLAMA_BASE_URL` | Used | Used (inherited) |
| `OPENAI_API_BASE_URL` | Defaults to `host.docker.internal:8081/v1` | Overridden to `openai-adapter:8000/v1` |
| `OPENAI_API_BASE_URLS` | Defaults to `host.docker.internal:8081/v1` | Overridden to `openai-adapter:8000/v1` |
| `OPENAI_API_KEY` / `OPENAI_API_KEYS` | Used | Used (inherited) |
| `ENABLE_OPENAI_API` / `ENABLE_OLLAMA_API` | Used | Used (inherited) |
| `WEBUI_SECRET_KEY` | Used | Used (inherited) |
| `OPENAI_ADAPTER_PORT` | Not used | Used |
| `ADAPTER_UPSTREAM_BASE_URL` | Not used | Used |
| `ADAPTER_UPSTREAM_API_KEY` | Not used | Used |
| `ADAPTER_UI_MODEL_ID` | Not used | Used |
| `ADAPTER_UPSTREAM_MODEL_ID` | Not used | Used |
| `ADAPTER_TIMEOUT_SECONDS` | Not used | Used |
| `ADAPTER_MODELS_TIMEOUT_SECONDS` | Not used | Used |
| `ADAPTER_API_KEY` | Not used | Used |

## Usage

Base stack only:

```bash
docker compose up -d
```

Base stack with OpenAI adapter:

```bash
docker compose -f docker-compose.yaml -f docker-compose.openai-adapter.yaml up -d --build
```

## Makefile Shortcuts

```bash
make up-base
make up-adapter
make ps-base
make ps-adapter
make logs-adapter
make down-base
make down
```

## Why this split

- Keeps default stack simple.
- Keeps adapter integration isolated and easier to maintain.
- Reduces accidental coupling between unrelated services.
