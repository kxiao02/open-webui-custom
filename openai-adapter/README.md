# OpenAI Adapter Service

This service exposes an OpenAI-compatible `/v1` API for OpenWebUI and forwards requests to an upstream LLM endpoint.

## Purpose

- Keep OpenWebUI integration stable by presenting a predictable OpenAI-compatible API.
- Isolate upstream model naming differences using model aliasing.
- Return structured errors when upstream fails.

## Endpoints

- `GET /health`
- `GET /v1/models`
- `GET /v1/models/{model_id}`
- `POST /v1/chat/completions` (supports streaming and non-streaming)

## Environment Variables

- `ADAPTER_UPSTREAM_BASE_URL` upstream OpenAI-compatible base URL (with `/v1`)
- `ADAPTER_UPSTREAM_API_KEY` optional upstream bearer token
- `ADAPTER_UI_MODEL_ID` model ID shown to OpenWebUI users
- `ADAPTER_UPSTREAM_MODEL_ID` model ID sent to upstream
- `ADAPTER_TIMEOUT_SECONDS` upstream request timeout
- `ADAPTER_MODELS_TIMEOUT_SECONDS` timeout for `/v1/models` upstream lookup
- `ADAPTER_API_KEY` optional inbound bearer token required by this adapter

## Model Aliasing

- Requests using `ADAPTER_UI_MODEL_ID` are sent upstream as `ADAPTER_UPSTREAM_MODEL_ID`.
- Upstream responses that include `ADAPTER_UPSTREAM_MODEL_ID` are rewritten to `ADAPTER_UI_MODEL_ID`.

## Recommended Defaults for Current Deployment

- `ADAPTER_UPSTREAM_BASE_URL=http://host.docker.internal:8081/v1`
- `ADAPTER_UI_MODEL_ID=miniagent-chat`
- `ADAPTER_UPSTREAM_MODEL_ID=deepagent`

## Quick Verify

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
```
