import json
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


ADAPTER_UPSTREAM_BASE_URL = _normalize_base_url(
    os.getenv("ADAPTER_UPSTREAM_BASE_URL", "http://host.docker.internal:8081/v1")
)
ADAPTER_UPSTREAM_API_KEY = os.getenv("ADAPTER_UPSTREAM_API_KEY", "")
ADAPTER_UI_MODEL_ID = os.getenv("ADAPTER_UI_MODEL_ID", "miniagent-chat")
ADAPTER_UPSTREAM_MODEL_ID = os.getenv("ADAPTER_UPSTREAM_MODEL_ID", "deepagent")
ADAPTER_TIMEOUT_SECONDS = float(os.getenv("ADAPTER_TIMEOUT_SECONDS", "120"))
ADAPTER_MODELS_TIMEOUT_SECONDS = float(os.getenv("ADAPTER_MODELS_TIMEOUT_SECONDS", "2"))
ADAPTER_API_KEY = os.getenv("ADAPTER_API_KEY", "")


app = FastAPI(title="OpenAI Adapter", version="1.0.0")


@app.on_event("startup")
async def startup_event() -> None:
    timeout = httpx.Timeout(ADAPTER_TIMEOUT_SECONDS)
    app.state.http_client = httpx.AsyncClient(timeout=timeout)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await app.state.http_client.aclose()


def _require_inbound_auth(authorization: str | None) -> None:
    if not ADAPTER_API_KEY:
        return

    expected = f"Bearer {ADAPTER_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid adapter API key")


def _upstream_headers(inbound_authorization: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if ADAPTER_UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {ADAPTER_UPSTREAM_API_KEY}"
    elif inbound_authorization:
        headers["Authorization"] = inbound_authorization

    return headers


def _map_model_to_upstream(requested_model: str | None) -> str:
    if not requested_model or requested_model == ADAPTER_UI_MODEL_ID:
        return ADAPTER_UPSTREAM_MODEL_ID
    return requested_model


def _map_model_to_ui(model_id: str | None) -> str | None:
    if model_id == ADAPTER_UPSTREAM_MODEL_ID:
        return ADAPTER_UI_MODEL_ID
    return model_id


def _model_descriptor(metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    created = int(time.time())
    object_type = "model"
    owned_by = "openai-adapter"

    if metadata:
        created = int(metadata.get("created", created))
        object_type = str(metadata.get("object", object_type))
        owned_by = str(metadata.get("owned_by", owned_by))

    return {
        "id": ADAPTER_UI_MODEL_ID,
        "object": object_type,
        "created": created,
        "owned_by": owned_by,
    }


def _decode_upstream_error_body(raw_content: bytes) -> tuple[str, Any]:
    text = raw_content.decode("utf-8", errors="replace").strip()
    if not text:
        return "Upstream request failed", None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        truncated = text[:1000]
        return truncated, truncated

    if isinstance(parsed, dict):
        if isinstance(parsed.get("error"), dict):
            message = str(parsed["error"].get("message", "Upstream request failed"))
            return message, parsed["error"]

        message = parsed.get("message")
        if isinstance(message, str) and message:
            return message, parsed

    return "Upstream request failed", parsed


def _error_response(
    *,
    status_code: int,
    message: str,
    upstream_status: int | None = None,
    details: Any = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "upstream_error" if status_code >= 500 else "request_error",
        }
    }

    if upstream_status is not None:
        payload["error"]["upstream_status"] = upstream_status
    if details is not None:
        payload["error"]["details"] = details

    return JSONResponse(status_code=status_code, content=payload)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "upstream_base_url": ADAPTER_UPSTREAM_BASE_URL,
        "ui_model_id": ADAPTER_UI_MODEL_ID,
        "upstream_model_id": ADAPTER_UPSTREAM_MODEL_ID,
    }


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_inbound_auth(authorization)

    upstream_url = f"{ADAPTER_UPSTREAM_BASE_URL}/models"
    try:
        upstream_response = await app.state.http_client.get(
            upstream_url,
            headers=_upstream_headers(authorization),
            timeout=httpx.Timeout(ADAPTER_MODELS_TIMEOUT_SECONDS),
        )
    except (httpx.TimeoutException, httpx.RequestError):
        return JSONResponse(content={"object": "list", "data": [_model_descriptor()]})

    if upstream_response.status_code >= 400:
        return JSONResponse(content={"object": "list", "data": [_model_descriptor()]})

    metadata: dict[str, Any] | None = None
    try:
        payload = upstream_response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict) and item.get("id") == ADAPTER_UPSTREAM_MODEL_ID:
                metadata = item
                break

        if metadata is None:
            for item in payload["data"]:
                if isinstance(item, dict):
                    metadata = item
                    break

    return JSONResponse(content={"object": "list", "data": [_model_descriptor(metadata)]})


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_inbound_auth(authorization)

    if model_id != ADAPTER_UI_MODEL_ID:
        return _error_response(status_code=404, message=f"Model '{model_id}' not found")

    return JSONResponse(content=_model_descriptor())


def _rewrite_stream_line(raw_line: str) -> bytes:
    if raw_line == "":
        return b"\n"

    if not raw_line.startswith("data:"):
        return f"{raw_line}\n".encode("utf-8")

    data_payload = raw_line[5:].strip()
    if data_payload == "[DONE]":
        return b"data: [DONE]\n\n"

    try:
        parsed = json.loads(data_payload)
    except json.JSONDecodeError:
        return f"{raw_line}\n".encode("utf-8")

    if isinstance(parsed, dict) and "model" in parsed:
        parsed["model"] = _map_model_to_ui(str(parsed["model"]))

    rewritten = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return f"data: {rewritten}\n\n".encode("utf-8")


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_inbound_auth(authorization)

    try:
        payload = await request.json()
    except ValueError:
        return _error_response(
            status_code=400,
            message="Request body must be valid JSON",
            details={"endpoint": "/chat/completions"},
        )

    if not isinstance(payload, dict):
        return _error_response(
            status_code=400,
            message="Request body must be a JSON object",
            details={"endpoint": "/chat/completions"},
        )

    payload["model"] = _map_model_to_upstream(payload.get("model"))
    stream_mode = bool(payload.get("stream"))
    upstream_url = f"{ADAPTER_UPSTREAM_BASE_URL}/chat/completions"

    if stream_mode:
        try:
            upstream_request = app.state.http_client.build_request(
                "POST",
                upstream_url,
                headers=_upstream_headers(authorization),
                json=payload,
            )
            upstream_response = await app.state.http_client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return _error_response(
                status_code=504,
                message="Upstream request timed out",
                details={"endpoint": "/chat/completions"},
            )
        except httpx.RequestError as exc:
            return _error_response(
                status_code=502,
                message="Unable to reach upstream endpoint",
                details={"reason": str(exc)},
            )

        if upstream_response.status_code >= 400:
            raw_body = await upstream_response.aread()
            await upstream_response.aclose()
            message, details = _decode_upstream_error_body(raw_body)
            return _error_response(
                status_code=upstream_response.status_code,
                message=message,
                upstream_status=upstream_response.status_code,
                details=details,
            )

        async def stream_generator():
            try:
                async for line in upstream_response.aiter_lines():
                    yield _rewrite_stream_line(line)
            finally:
                await upstream_response.aclose()

        media_type = upstream_response.headers.get("content-type", "text/event-stream")
        return StreamingResponse(stream_generator(), media_type=media_type)

    try:
        upstream_response = await app.state.http_client.post(
            upstream_url,
            headers=_upstream_headers(authorization),
            json=payload,
        )
    except httpx.TimeoutException:
        return _error_response(
            status_code=504,
            message="Upstream request timed out",
            details={"endpoint": "/chat/completions"},
        )
    except httpx.RequestError as exc:
        return _error_response(
            status_code=502,
            message="Unable to reach upstream endpoint",
            details={"reason": str(exc)},
        )

    if upstream_response.status_code >= 400:
        message, details = _decode_upstream_error_body(upstream_response.content)
        return _error_response(
            status_code=upstream_response.status_code,
            message=message,
            upstream_status=upstream_response.status_code,
            details=details,
        )

    try:
        response_payload = upstream_response.json()
    except ValueError:
        return _error_response(
            status_code=502,
            message="Upstream returned non-JSON response",
            details={"endpoint": "/chat/completions"},
        )

    if isinstance(response_payload, dict) and "model" in response_payload:
        response_payload["model"] = _map_model_to_ui(str(response_payload["model"]))

    return JSONResponse(status_code=upstream_response.status_code, content=response_payload)
