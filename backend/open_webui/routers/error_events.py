import json

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from open_webui.utils.error_events import (
    _get_max_event_bytes,
    forward_error_event,
    sanitize_error_event,
)

router = APIRouter()


@router.post("/")
async def ingest_browser_error_event(request: Request) -> JSONResponse:
    body = await request.body()
    if len(body) > _get_max_event_bytes():
        # Do not persist locally; tell the browser adapter it was dropped.
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "dropped", "reason": "too_large"},
        )

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON event"
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Event must be a JSON object",
        )

    event = sanitize_error_event(payload)
    result = await forward_error_event(event, request=request, service="open-webui")
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)
