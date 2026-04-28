from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_webui.internal.db import get_session
from open_webui.utils.auth import get_verified_user
from open_webui.utils.knowflow import (
    KnowflowError,
    clear_manual_binding,
    get_binding_status,
    get_public_read_only_api_key,
    is_knowflow_enabled,
    resolve_knowflow_asset_url,
    save_manual_binding,
)

router = APIRouter()
asset_router = APIRouter()
api_asset_router = APIRouter()

_KNOWFLOW_PROXY_RESPONSE_HEADERS = (
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
)
_KNOWFLOW_PROXY_REQUEST_HEADERS = ("accept", "if-none-match", "if-modified-since", "range")


class KnowflowBindingForm(BaseModel):
    api_key: str


class KnowflowBindingStatus(BaseModel):
    connected: bool
    status: str
    mode: str
    bind_required: bool
    manual_binding_present: bool = False
    public_binding_present: bool = False
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None


def _normalize_asset_path(asset_path: str) -> str:
    parts = []
    for raw_part in str(asset_path or "").split("/"):
        part = raw_part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Knowflow asset not found",
            )
        parts.append(part)

    normalized = "/".join(parts)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowflow asset not found",
        )

    return normalized


def _normalize_proxy_asset_path(asset_path: str) -> str:
    normalized = str(asset_path or "").strip().lstrip("/")
    lowered = normalized.lower()
    if lowered.startswith("openai/minio/"):
        normalized = normalized[len("openai/minio/") :]
    elif lowered.startswith("minio/"):
        normalized = normalized[len("minio/") :]
    return _normalize_asset_path(normalized)


def _build_proxy_response_headers(response: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header_name in _KNOWFLOW_PROXY_RESPONSE_HEADERS:
        header_value = response.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value
    return headers


@router.get("/status", response_model=KnowflowBindingStatus)
async def get_knowflow_status(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    payload = await get_binding_status(request.app.state.config, user, db=db)
    return KnowflowBindingStatus(**payload)


@router.get("/binding", response_model=KnowflowBindingStatus)
async def get_knowflow_binding(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    payload = await get_binding_status(request.app.state.config, user, db=db)
    return KnowflowBindingStatus(**payload)


@router.post("/binding", response_model=KnowflowBindingStatus)
async def bind_knowflow_api_key(
    request: Request,
    form_data: KnowflowBindingForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    try:
        payload = await save_manual_binding(
            request.app.state.config, user.id, form_data.api_key, db=db
        )
        return KnowflowBindingStatus(
            **payload,
            manual_binding_present=True,
            public_binding_present=bool(
                get_public_read_only_api_key(request.app.state.config)
            ),
        )
    except KnowflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.delete("/binding", response_model=KnowflowBindingStatus)
async def clear_knowflow_api_key_binding(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    clear_manual_binding(user.id, db=db)
    payload = await get_binding_status(request.app.state.config, user, db=db)
    return KnowflowBindingStatus(**payload)


async def _proxy_knowflow_asset(
    asset_path: str,
    request: Request,
):
    if not is_knowflow_enabled(request.app.state.config):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowflow asset not found",
        )

    normalized_asset_path = _normalize_proxy_asset_path(asset_path)
    upstream_url = resolve_knowflow_asset_url(
        request.app.state.config,
        f"/minio/{normalized_asset_path}",
    )
    if not upstream_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowflow asset not found",
        )

    request_headers: dict[str, str] = {}
    for header_name in _KNOWFLOW_PROXY_REQUEST_HEADERS:
        header_value = request.headers.get(header_name)
        if header_value:
            request_headers[header_name] = header_value

    timeout_seconds = max(
        1,
        int(getattr(request.app.state.config, "KNOWFLOW_TIMEOUT_SECONDS", 10) or 10),
    )

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=True,
        ) as client:
            upstream_response = await client.request(
                request.method,
                upstream_url,
                headers=request_headers,
                params=request.query_params,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Knowflow asset request failed: {exc}",
        ) from exc

    response_headers = _build_proxy_response_headers(upstream_response)
    response_content = b"" if request.method == "HEAD" else upstream_response.content

    return Response(
        content=response_content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


@asset_router.api_route("/minio/{asset_path:path}", methods=["GET", "HEAD"])
async def proxy_knowflow_asset(
    asset_path: str,
    request: Request,
):
    return await _proxy_knowflow_asset(asset_path, request)


@api_asset_router.api_route(
    "/api/v1/knowflow/assets/{asset_path:path}", methods=["GET", "HEAD"]
)
async def proxy_knowflow_asset_api(
    asset_path: str,
    request: Request,
):
    return await _proxy_knowflow_asset(asset_path, request)
