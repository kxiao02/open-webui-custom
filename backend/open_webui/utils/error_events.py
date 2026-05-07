import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

import httpx
from fastapi import Request

log = logging.getLogger(__name__)

REDACTION_MARKER = "[REDACTED]"
SENSITIVE_KEY_RE = re.compile(
    r"(^|[_-])(authorization|cookie|set_cookie|token|access_token|refresh_token|id_token|api_key|apikey|secret|password|passwd|prompt|prompts|input|inputs|file_content|uploaded_content|raw_body|request_body|response_body|html_body|body|html)($|[_-])",
    re.IGNORECASE,
)
MAX_STRING_LENGTH = 2048


def _get_error_log_service_url() -> str:
    return os.getenv("ERROR_LOG_SERVICE_URL", "").strip().rstrip("/")


def _get_error_log_api_key() -> str:
    return os.getenv("ERROR_LOG_SERVICE_API_KEY", "").strip()


def _get_timeout_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("ERROR_LOG_FORWARD_TIMEOUT_SECONDS", "1.5")))
    except ValueError:
        return 1.5


def _get_max_event_bytes() -> int:
    try:
        return max(1024, int(os.getenv("ERROR_LOG_MAX_EVENT_BYTES", "65536")))
    except ValueError:
        return 65536


def _truncate(value: str, limit: int = MAX_STRING_LENGTH) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]"


def sanitize_error_event(value: Any, *, key: str = "") -> Any:
    if key and SENSITIVE_KEY_RE.search(key):
        return REDACTION_MARKER

    if isinstance(value, dict):
        return {
            str(item_key): sanitize_error_event(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }

    if isinstance(value, list):
        return [sanitize_error_event(item) for item in value[:100]]

    if isinstance(value, str):
        return _truncate(value)

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return _truncate(str(value))


def _hash_value(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def build_error_event_context(request: Optional[Request]) -> dict[str, Any]:
    if request is None:
        return {}

    headers = request.headers
    client_host = request.client.host if request.client else ""
    user_agent = headers.get("user-agent", "")
    return {
        "request": {
            "method": request.method,
            "path": request.url.path,
            "query_present": bool(request.url.query),
            "request_id": headers.get("x-request-id")
            or headers.get("x-correlation-id"),
            "traceparent": headers.get("traceparent"),
            "referer": headers.get("referer"),
            "user_agent": user_agent,
            "client_hash": _hash_value(f"{client_host}|{user_agent}"),
        }
    }


def _bounded_payload(event: dict[str, Any]) -> dict[str, Any]:
    max_bytes = _get_max_event_bytes()
    encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= max_bytes:
        return event

    bounded = dict(event)
    bounded["truncated"] = True
    bounded["payload"] = "[TRUNCATED]"
    encoded = json.dumps(bounded, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= max_bytes:
        return bounded

    return {
        "type": str(event.get("type") or "error_event"),
        "truncated": True,
        "message": _truncate(
            str(event.get("message") or "Error event exceeded max size"), 512
        ),
    }


async def forward_error_event(
    event: dict[str, Any],
    *,
    request: Optional[Request] = None,
    service: str = "open-webui",
) -> dict[str, Any]:
    service_url = _get_error_log_service_url()
    if not service_url:
        return {"status": "disabled"}

    if request is not None and request.url.path.endswith("/error-events"):
        return {"status": "skipped"}

    sanitized = sanitize_error_event(event)
    if not isinstance(sanitized, dict):
        sanitized = {"message": str(sanitized)}

    payload = _bounded_payload(
        {
            "service": service,
            "source": "backend-adapter",
            **build_error_event_context(request),
            **sanitized,
        }
    )

    headers = {"Content-Type": "application/json"}
    api_key = _get_error_log_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(
            timeout=_get_timeout_seconds(), trust_env=True
        ) as client:
            response = await client.post(
                f"{service_url}/v1/events", json=payload, headers=headers
            )
        if response.status_code >= 400:
            log.debug(
                "Error event forwarding dropped with status %s", response.status_code
            )
            return {"status": "dropped", "reason": f"upstream_{response.status_code}"}
        return {"status": "forwarded"}
    except Exception as exc:
        log.debug("Error event forwarding failed: %s", exc)
        return {"status": "dropped", "reason": "forward_failed"}
