import time
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException, status

from open_webui.models.oauth_sessions import OAuthSessions

log = logging.getLogger(__name__)

KNOWFLOW_BINDING_PROVIDER = "knowflow_manual_bind"
KNOWFLOW_BINDING_EXPIRES_AT = 4102444800  # 2100-01-01T00:00:00Z


class KnowflowError(Exception):
    pass


@dataclass
class KnowflowContext:
    mode: str
    api_key: str
    knowflow_user_id: Optional[str] = None
    knowflow_user_email: Optional[str] = None
    knowflow_user_name: Optional[str] = None
    is_public: bool = False


def _get_config_value(config: Any, key: str, default=None):
    value = getattr(config, key, default)
    if hasattr(value, "value"):
        return value.value
    return value


def get_knowflow_site_url(config: Any) -> str:
    site_url = str(_get_config_value(config, "KNOWFLOW_SITE_URL", "") or "").strip()
    if site_url:
        return site_url.rstrip("/")

    server_url = str(_get_config_value(config, "KNOWFLOW_SERVER_BASE_URL", "") or "").strip()
    if not server_url:
        return ""

    parsed = urlparse(server_url)
    if not parsed.scheme or not parsed.netloc:
        return server_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.hostname or parsed.netloc}".rstrip("/")


def get_knowflow_public_config(config: Any) -> dict[str, Any]:
    return {
        "enabled": is_knowflow_enabled(config),
        "site_url": get_knowflow_site_url(config),
        "read_only": bool(_get_config_value(config, "KNOWFLOW_READ_ONLY", True)),
        "public_read_only_enabled": bool(get_public_read_only_api_key(config)),
        "manual_bind_enabled": bool(
            _get_config_value(config, "KNOWFLOW_MANUAL_BINDING_ENABLED", True)
        ),
        "managed_lookup_enabled": bool(
            _get_config_value(config, "KNOWFLOW_MANAGED_LOOKUP_ENABLED", True)
        ),
    }


def is_knowflow_enabled(config: Any) -> bool:
    return bool(
        str(_get_config_value(config, "KNOWFLOW_SERVER_BASE_URL", "") or "").strip()
        and str(_get_config_value(config, "KNOWFLOW_RAGFLOW_BASE_URL", "") or "").strip()
    )


def is_knowflow_read_only(config: Any) -> bool:
    return bool(_get_config_value(config, "KNOWFLOW_READ_ONLY", True))


def get_public_read_only_api_key(config: Any) -> str:
    return str(
        _get_config_value(config, "KNOWFLOW_PUBLIC_READ_ONLY_API_KEY", "") or ""
    ).strip()


def get_manual_binding_session(user_id: str, db=None):
    return OAuthSessions.get_session_by_provider_and_user_id(
        KNOWFLOW_BINDING_PROVIDER, user_id, db=db
    )


_KNOWFLOW_CHUNK_RENDER_URL_FIELDS = (
    "url",
    "image_url",
    "html_url",
    "render_url",
    "asset_url",
    "content_url",
)
_KNOWFLOW_CHUNK_EMBED_URL_FIELDS = ("embed_url", "preview_url")
_KNOWFLOW_CHUNK_IMAGE_ID_FIELDS = ("image_id", "img_id")
_KNOWFLOW_CHUNK_HTML_FIELDS = (
    "html",
    "html_content",
    "table_html",
    "render_html",
    "content_html",
)
_KNOWFLOW_CHUNK_MARKDOWN_FIELDS = (
    "markdown",
    "md",
    "table_markdown",
    "render_markdown",
)
_KNOWFLOW_CHUNK_PAGE_FIELDS = ("page", "page_num", "page_number")
_KNOWFLOW_CHUNK_TYPE_FIELDS = ("chunk_type", "type", "content_type")

_KNOWFLOW_INLINE_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_KNOWFLOW_INLINE_IMG_SRC_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*(['\"])(?P<src>.+?)\1",
    re.IGNORECASE | re.DOTALL,
)
_KNOWFLOW_INLINE_TABLE_RE = re.compile(
    r"<table\b.*?</table>",
    re.IGNORECASE | re.DOTALL,
)
_KNOWFLOW_HTML_URL_ATTR_RE = re.compile(
    r'(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>[\'"])(?P<url>.+?)(?P=quote)',
    re.IGNORECASE | re.DOTALL,
)
_KNOWFLOW_MINIO_PATH_RE = re.compile(
    r"(?P<path>/(?:openai/)?minio/[^?#]+)",
    re.IGNORECASE,
)


def _first_non_empty_str(values) -> Optional[str]:
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _first_non_none(values):
    for value in values:
        if value is not None:
            return value
    return None


def _normalize_optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(float(normalized))
        except ValueError:
            return None
    return None


def is_knowflow_image_ref(value: Optional[str]) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False

    lowered = normalized.lower()
    if lowered.startswith("data:image/"):
        return True

    parsed = urlparse(normalized)
    path = (parsed.path or normalized).lower()
    return path.endswith(
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".tif",
            ".tiff",
            ".avif",
        )
    )


def get_knowflow_asset_ref_key(value: Optional[str]) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""

    lowered = normalized.lower()
    if lowered.startswith(("data:", "blob:")):
        return ""

    parsed = urlparse(normalized)
    path = parsed.path or normalized
    if not path:
        return ""

    if not path.startswith("/"):
        path = f"/{path.lstrip('/')}"

    match = _KNOWFLOW_MINIO_PATH_RE.search(path)
    if not match:
        return ""

    asset_path = match.group("path").strip()
    if not asset_path:
        return ""

    asset_path = re.sub(r"^/openai(?=/minio/)", "", asset_path, flags=re.IGNORECASE)
    asset_path = re.sub(r"/{2,}", "/", asset_path)
    return asset_path


def resolve_knowflow_asset_url(config: Any, value: Optional[str]) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""

    lowered = normalized.lower()
    if lowered.startswith(("data:", "blob:")):
        return normalized

    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc:
        return normalized

    base_url = get_knowflow_site_url(config)
    if not base_url:
        return normalized

    return urljoin(f"{base_url.rstrip('/')}/", normalized)


def resolve_knowflow_html_content(config: Any, value: Optional[str]) -> str:
    html = str(value or "").strip()
    if not html or "<" not in html:
        return html

    def _replace(match: re.Match[str]) -> str:
        resolved_url = resolve_knowflow_asset_url(config, match.group("url"))
        if not resolved_url:
            return match.group(0)
        return f"{match.group('prefix')}{match.group('quote')}{resolved_url}{match.group('quote')}"

    return _KNOWFLOW_HTML_URL_ATTR_RE.sub(_replace, html)


def build_knowflow_markdown_image(url: str, alt_text: str = "Knowledge image") -> str:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return ""

    normalized_alt = str(alt_text or "Knowledge image").replace("[", "").replace("]", "").strip()
    if not normalized_alt:
        normalized_alt = "Knowledge image"
    return f"![{normalized_alt}]({normalized_url})"


def get_knowflow_chunk_content(chunk: dict[str, Any]) -> str:
    return str(
        chunk.get("content")
        or chunk.get("text")
        or chunk.get("chunk")
        or chunk.get("body")
        or ""
    ).strip()


def knowflow_content_has_inline_visuals(content: str) -> bool:
    normalized = str(content or "").lower()
    return "<img" in normalized or "<table" in normalized


def _extract_inline_render_metadata(content: str) -> dict[str, Any]:
    normalized = str(content or "").strip()
    if "<" not in normalized:
        return {}

    image_tags = [match.group(0).strip() for match in _KNOWFLOW_INLINE_IMG_TAG_RE.finditer(normalized)]
    table_tags = [match.group(0).strip() for match in _KNOWFLOW_INLINE_TABLE_RE.finditer(normalized)]

    metadata: dict[str, Any] = {}

    if image_tags:
        src_match = _KNOWFLOW_INLINE_IMG_SRC_RE.search(image_tags[0])
        if src_match:
            render_url = str(src_match.group("src") or "").strip()
            if render_url:
                metadata["url"] = render_url

    inline_html_fragments: list[str] = []
    if table_tags:
        inline_html_fragments.extend(table_tags)
    elif image_tags and "url" not in metadata:
        inline_html_fragments.extend(image_tags)

    if inline_html_fragments:
        metadata["html"] = True
        metadata["html_content"] = "\n\n".join(inline_html_fragments)

    return metadata


def get_knowflow_chunk_file_id(
    chunk: dict[str, Any], *, fallback_file_id: str = ""
) -> str:
    return str(
        chunk.get("document_id")
        or chunk.get("doc_id")
        or fallback_file_id
        or ""
    ).strip()


def get_knowflow_chunk_source_name(
    chunk: dict[str, Any], *, fallback_name: str = ""
) -> str:
    return str(
        chunk.get("document_name")
        or chunk.get("docnm_kwd")
        or chunk.get("source")
        or chunk.get("filename")
        or chunk.get("title")
        or fallback_name
        or get_knowflow_chunk_file_id(chunk)
        or "Unknown"
    ).strip()


def get_knowflow_chunk_similarity(chunk: dict[str, Any]) -> Any:
    similarity = chunk.get("similarity")
    if similarity is None:
        similarity = chunk.get("score")
    return similarity


def get_knowflow_chunk_render_metadata(
    chunk: dict[str, Any], config: Any | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    content = get_knowflow_chunk_content(chunk)

    render_url = _first_non_empty_str(chunk.get(field) for field in _KNOWFLOW_CHUNK_RENDER_URL_FIELDS)
    if render_url:
        metadata["url"] = render_url

    embed_url = _first_non_empty_str(chunk.get(field) for field in _KNOWFLOW_CHUNK_EMBED_URL_FIELDS)
    if embed_url:
        metadata["embed_url"] = embed_url

    image_id = _first_non_empty_str(chunk.get(field) for field in _KNOWFLOW_CHUNK_IMAGE_ID_FIELDS)
    if image_id:
        metadata["image_id"] = image_id

    html_content = _first_non_empty_str(chunk.get(field) for field in _KNOWFLOW_CHUNK_HTML_FIELDS)
    if html_content:
        metadata["html"] = True
        metadata["html_content"] = html_content

    render_markdown = _first_non_empty_str(
        chunk.get(field) for field in _KNOWFLOW_CHUNK_MARKDOWN_FIELDS
    )
    if render_markdown:
        metadata["render_markdown"] = render_markdown

    chunk_type = _first_non_empty_str(chunk.get(field) for field in _KNOWFLOW_CHUNK_TYPE_FIELDS)
    if chunk_type:
        metadata["chunk_type"] = chunk_type

    page = _normalize_optional_int(
        _first_non_none(chunk.get(field) for field in _KNOWFLOW_CHUNK_PAGE_FIELDS)
    )
    if page is not None:
        metadata["page"] = page

    if content and ("url" not in metadata or "html_content" not in metadata):
        inline_metadata = _extract_inline_render_metadata(content)
        if "url" not in metadata and inline_metadata.get("url"):
            metadata["url"] = inline_metadata["url"]
        if "html_content" not in metadata and inline_metadata.get("html_content"):
            metadata["html"] = True
            metadata["html_content"] = inline_metadata["html_content"]

    if metadata.get("url"):
        metadata["url"] = resolve_knowflow_asset_url(config, metadata.get("url"))
    if metadata.get("embed_url"):
        metadata["embed_url"] = resolve_knowflow_asset_url(
            config, metadata.get("embed_url")
        )
    if metadata.get("html_content"):
        metadata["html_content"] = resolve_knowflow_html_content(
            config, metadata.get("html_content")
        )

    return metadata


class KnowflowClient:
    def __init__(self, config: Any, api_key: str):
        self.server_base_url = str(
            _get_config_value(config, "KNOWFLOW_SERVER_BASE_URL", "") or ""
        ).rstrip("/")
        self.ragflow_base_url = str(
            _get_config_value(config, "KNOWFLOW_RAGFLOW_BASE_URL", "") or ""
        ).rstrip("/")
        self.timeout_seconds = int(
            _get_config_value(config, "KNOWFLOW_TIMEOUT_SECONDS", 10) or 10
        )
        self.api_key = api_key.strip()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not base_url:
            raise KnowflowError("Knowflow base URL is not configured")

        url = f"{base_url}{path}"
        timeout = httpx.Timeout(self.timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_data,
                )
            except httpx.HTTPError as exc:
                raise KnowflowError(f"Knowflow request failed: {exc}") from exc

        payload = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = response.json()
            except ValueError:
                payload = None

        if response.status_code == 401:
            raise KnowflowError("Knowflow authentication failed")
        if response.status_code == 403:
            raise KnowflowError("Knowflow permission denied")
        if response.status_code >= 400:
            detail = None
            if isinstance(payload, dict):
                detail = payload.get("details") or payload.get("message")
            raise KnowflowError(detail or f"Knowflow request failed with {response.status_code}")

        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise KnowflowError(payload.get("details") or payload.get("message") or "Knowflow request failed")

        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    async def get_current_user(self) -> dict[str, Any]:
        data = await self._request(self.server_base_url, "/api/v1/users/current")
        return data or {}

    async def validate_api_key(self) -> None:
        # The deployed Knowflow build does not expose a stable current-user endpoint,
        # but listing datasets is enough to verify that the API key is accepted.
        await self.list_datasets(page=1, page_size=1)

    async def search_users_by_email(self, email: str) -> list[dict[str, Any]]:
        data = await self._request(
            self.server_base_url,
            "/api/v1/admin/users",
            params={"page": 1, "page_size": 20, "email": email},
        )
        if isinstance(data, dict):
            return data.get("users") or []
        return data or []

    async def get_user_roles(
        self, user_id: str, resource_type: str = "kb"
    ) -> list[dict[str, Any]]:
        data = await self._request(
            self.server_base_url,
            f"/api/v1/rbac/users/{user_id}/roles",
            params={"resource_type": resource_type},
        )
        if isinstance(data, dict):
            return data.get("roles") or []
        return data or []

    async def check_permission(
        self,
        permission: str,
        resource_type: str,
        resource_id: str,
        *,
        user_id: Optional[str] = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "permission": permission,
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        if user_id:
            payload["user_id"] = user_id

        data = await self._request(
            self.server_base_url,
            "/api/v1/rbac/permissions/check",
            method="POST",
            json_data=payload,
        )
        return bool((data or {}).get("has_permission"))

    async def list_datasets(
        self,
        *,
        page: int = 1,
        page_size: int = 1024,
        name: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "orderby": "update_time",
            "desc": True,
        }
        if name:
            params["name"] = name
        if dataset_id:
            params["id"] = dataset_id

        data = await self._request(
            self.ragflow_base_url,
            "/api/v1/datasets",
            params=params,
        )
        return data or []

    async def list_documents(
        self,
        dataset_id: str,
        *,
        page: int = 1,
        page_size: int = 1024,
        keywords: Optional[str] = None,
        document_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if keywords:
            params["keywords"] = keywords
        if document_id:
            params["id"] = document_id
        if name:
            params["name"] = name

        data = await self._request(
            self.ragflow_base_url,
            f"/api/v1/datasets/{dataset_id}/documents",
            params=params,
        )
        return data or {"docs": [], "total": 0}

    async def retrieve(
        self,
        question: str,
        *,
        dataset_ids: Optional[list[str]] = None,
        document_ids: Optional[list[str]] = None,
        page_size: int = 5,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "question": question,
            "page": 1,
            "page_size": page_size,
            "highlight": True,
        }
        if dataset_ids:
            payload["dataset_ids"] = dataset_ids
        if document_ids:
            payload["document_ids"] = document_ids

        data = await self._request(
            self.ragflow_base_url,
            "/api/v1/retrieval",
            method="POST",
            json_data=payload,
        )

        if isinstance(data, dict):
            if isinstance(data.get("reference"), dict):
                return data["reference"].get("chunks") or []
            return data.get("chunks") or []
        return []


def _normalize_role_map(roles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    role_map: dict[str, dict[str, Any]] = {}
    for role in roles or []:
        resource_id = str(role.get("resource_id") or "").strip()
        if not resource_id:
            continue
        role_map[resource_id] = {
            "role_name": role.get("role_name"),
            "permissions": set(role.get("permissions") or []),
        }
    return role_map


def _normalize_dataset_item(
    dataset: dict[str, Any],
    *,
    role_map: Optional[dict[str, dict[str, Any]]] = None,
    current_user_id: Optional[str] = None,
    source_mode: Optional[str] = None,
) -> dict[str, Any]:
    dataset_id = str(dataset.get("id") or dataset.get("dataset_id") or "").strip()
    role_info = (role_map or {}).get(dataset_id, {})
    permissions = role_info.get("permissions") or set()
    created_by = str(dataset.get("created_by") or "").strip()
    is_public = source_mode == "public"
    is_shared = bool(current_user_id and created_by and created_by != current_user_id)
    if is_public:
        is_shared = True
        write_access = False
    else:
        write_access = bool(current_user_id and created_by == current_user_id) or any(
            permission in permissions
            for permission in ("kb.edit", "kb.manage_access", "kb.delete")
        ) or role_info.get("role_name") in {"owner", "kb_owner", "kb_editor"}

    updated_at = dataset.get("update_time") or dataset.get("create_time") or int(
        time.time() * 1000
    )

    return {
        "id": dataset_id,
        "user_id": created_by or current_user_id or "",
        "name": dataset.get("name") or "",
        "description": dataset.get("description") or "",
        "meta": {
            "document_count": dataset.get("document_count"),
            "chunk_count": dataset.get("chunk_count"),
            "created_by": created_by,
            "status": dataset.get("status"),
            "source_mode": source_mode,
        },
        "access_grants": [],
        "created_at": int((dataset.get("create_time") or updated_at) / 1000),
        "updated_at": int(updated_at / 1000),
        "write_access": write_access,
        "visibility": "public" if is_public else ("shared" if is_shared else "private"),
        "is_shared": is_shared,
        "files_count": dataset.get("document_count"),
    }


def _matches_knowledge_query(item: dict[str, Any], query: Optional[str]) -> bool:
    needle = str(query or "").strip().casefold()
    if not needle:
        return True

    meta = item.get("meta") or {}
    haystacks = (
        str(item.get("id") or ""),
        str(item.get("name") or ""),
        str(item.get("description") or ""),
        str(meta.get("created_by") or ""),
    )
    return any(needle in value.casefold() for value in haystacks if value)


def _infer_manual_binding_user_id(
    datasets: list[dict[str, Any]], context: KnowflowContext
) -> Optional[str]:
    if context.knowflow_user_id or context.mode != "manual_bind":
        return context.knowflow_user_id

    owner_ids = {
        str(dataset.get("created_by") or "").strip()
        for dataset in datasets or []
        if str(dataset.get("created_by") or "").strip()
    }
    if len(owner_ids) == 1:
        return next(iter(owner_ids))
    return None


def _normalize_document_item(
    document: dict[str, Any],
    *,
    current_user_id: str,
    dataset: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    created_at = document.get("create_time") or int(time.time() * 1000)
    updated_at = document.get("update_time") or created_at
    name = document.get("name") or document.get("filename") or document.get("title") or ""
    result = {
        "id": str(document.get("id") or document.get("document_id") or "").strip(),
        "user_id": current_user_id,
        "filename": name,
        "data": None,
        "meta": {
            "name": name,
            "size": document.get("size"),
            "chunk_count": document.get("chunk_count"),
            "run": document.get("run"),
            "progress": document.get("progress"),
        },
        "created_at": int(created_at / 1000),
        "updated_at": int(updated_at / 1000),
    }
    if dataset:
        result["collection"] = {
            "id": dataset.get("id"),
            "name": dataset.get("name"),
        }
    return result


async def resolve_managed_context(config: Any, user) -> Optional[KnowflowContext]:
    service_api_key = str(_get_config_value(config, "KNOWFLOW_SERVICE_API_KEY", "") or "").strip()
    if not service_api_key or not bool(
        _get_config_value(config, "KNOWFLOW_MANAGED_LOOKUP_ENABLED", True)
    ):
        return None

    if not getattr(user, "email", None):
        return None

    client = KnowflowClient(config, service_api_key)
    users = await client.search_users_by_email(user.email.strip().lower())
    matched_user = next(
        (
            item
            for item in users
            if str(item.get("email") or "").strip().lower() == user.email.strip().lower()
        ),
        None,
    )
    if matched_user is None:
        return None

    return KnowflowContext(
        mode="managed",
        api_key=service_api_key,
        knowflow_user_id=str(matched_user.get("id") or "").strip() or None,
        knowflow_user_email=str(matched_user.get("email") or "").strip() or None,
        knowflow_user_name=str(matched_user.get("nickname") or "").strip() or None,
    )


def resolve_public_read_only_context(config: Any) -> Optional[KnowflowContext]:
    api_key = get_public_read_only_api_key(config)
    if not api_key:
        return None

    return KnowflowContext(
        mode="public",
        api_key=api_key,
        is_public=True,
    )


async def resolve_user_knowflow_context(
    config: Any, user, db=None
) -> Optional[KnowflowContext]:
    try:
        managed_context = await resolve_managed_context(config, user)
    except KnowflowError as exc:
        log.warning(
            "Knowflow managed user lookup failed; falling back to other bindings: %s",
            exc,
        )
        managed_context = None

    if managed_context is not None:
        return managed_context

    session = get_manual_binding_session(user.id, db=db)
    if not session:
        return None

    token = session.token or {}
    api_key = str(token.get("api_key") or "").strip()
    if not api_key:
        return None

    return KnowflowContext(
        mode="manual_bind",
        api_key=api_key,
        knowflow_user_id=str(token.get("knowflow_user_id") or "").strip() or None,
        knowflow_user_email=str(token.get("knowflow_user_email") or "").strip() or None,
        knowflow_user_name=str(token.get("knowflow_user_name") or "").strip() or None,
    )


async def resolve_knowflow_context(config: Any, user, db=None) -> Optional[KnowflowContext]:
    user_context = await resolve_user_knowflow_context(config, user, db=db)
    if user_context is not None:
        return user_context
    return resolve_public_read_only_context(config)


async def resolve_knowflow_contexts(config: Any, user, db=None) -> list[KnowflowContext]:
    contexts: list[KnowflowContext] = []
    user_context = await resolve_user_knowflow_context(config, user, db=db)
    public_context = resolve_public_read_only_context(config)

    for context in (user_context, public_context):
        if context is None:
            continue
        if any(existing.api_key == context.api_key for existing in contexts):
            continue
        contexts.append(context)

    return contexts


async def get_binding_status(config: Any, user, db=None) -> dict[str, Any]:
    manual_session = get_manual_binding_session(user.id, db=db)
    public_context = resolve_public_read_only_context(config)
    user_context = await resolve_user_knowflow_context(config, user, db=db)
    public_binding_present = public_context is not None

    if user_context is not None:
        return {
            "connected": True,
            "status": "connected",
            "mode": user_context.mode,
            "bind_required": False,
            "manual_binding_present": manual_session is not None,
            "public_binding_present": public_binding_present,
            "user_id": user_context.knowflow_user_id,
            "user_email": user_context.knowflow_user_email,
            "user_name": user_context.knowflow_user_name,
        }

    if public_context is not None:
        return {
            "connected": True,
            "status": "connected",
            "mode": public_context.mode,
            "bind_required": False,
            "manual_binding_present": False,
            "public_binding_present": True,
            "user_id": None,
            "user_email": None,
            "user_name": None,
        }

    return {
        "connected": False,
        "status": "not_bound",
        "mode": "manual_bind",
        "bind_required": True,
        "manual_binding_present": False,
        "public_binding_present": False,
        "user_id": None,
        "user_email": None,
        "user_name": None,
    }


async def save_manual_binding(config: Any, user_id: str, api_key: str, db=None) -> dict[str, Any]:
    api_key = api_key.strip()
    if not api_key:
        raise KnowflowError("Knowflow API key is required")

    client = KnowflowClient(config, api_key)
    current_user: dict[str, Any] = {}
    try:
        current_user = await client.get_current_user()
    except KnowflowError as exc:
        log.warning(
            "Knowflow current-user lookup failed during manual bind; "
            "falling back to dataset probe validation: %s",
            exc,
        )
        await client.validate_api_key()

    knowflow_user_id = str(current_user.get("id") or "").strip()
    if current_user and not knowflow_user_id:
        log.warning(
            "Knowflow current-user payload missing id during manual bind; "
            "keeping API-key-only binding"
        )

    token = {
        "api_key": api_key,
        "knowflow_user_id": knowflow_user_id or None,
        "knowflow_user_email": str(current_user.get("email") or "").strip(),
        "knowflow_user_name": str(current_user.get("nickname") or "").strip(),
        "validated_at": int(time.time()),
        "expires_at": KNOWFLOW_BINDING_EXPIRES_AT,
    }

    existing_session = get_manual_binding_session(user_id, db=db)
    if existing_session:
        OAuthSessions.update_session_by_id(existing_session.id, token, db=db)
    else:
        OAuthSessions.create_session(
            user_id=user_id,
            provider=KNOWFLOW_BINDING_PROVIDER,
            token=token,
            db=db,
        )

    return {
        "connected": True,
        "status": "connected",
        "mode": "manual_bind",
        "bind_required": False,
        "user_id": knowflow_user_id,
        "user_email": token.get("knowflow_user_email"),
        "user_name": token.get("knowflow_user_name"),
    }


def clear_manual_binding(user_id: str, db=None) -> bool:
    existing_session = get_manual_binding_session(user_id, db=db)
    if not existing_session:
        return True
    return OAuthSessions.delete_session_by_id(existing_session.id, db=db)


async def list_accessible_knowledge_bases(
    config: Any,
    user,
    *,
    query: Optional[str] = None,
    view_option: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
    db=None,
) -> dict[str, Any]:
    result = await _list_accessible_knowledge_bases_with_context(
        config,
        user,
        query=query,
        view_option=view_option,
        page=page,
        page_size=page_size,
        db=db,
    )
    result.pop("_contexts_by_id", None)
    return result


async def _list_accessible_knowledge_bases_with_context(
    config: Any,
    user,
    *,
    query: Optional[str] = None,
    view_option: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
    db=None,
) -> dict[str, Any]:
    contexts = await resolve_knowflow_contexts(config, user, db=db)
    if not contexts:
        return {"items": [], "total": 0, "mode": None}

    normalized_by_id: dict[str, dict[str, Any]] = {}
    contexts_by_id: dict[str, KnowflowContext] = {}

    for context in contexts:
        client = KnowflowClient(config, context.api_key)
        role_map: dict[str, dict[str, Any]] = {}

        if context.knowflow_user_id:
            try:
                role_map = _normalize_role_map(
                    await client.get_user_roles(
                        context.knowflow_user_id, resource_type="kb"
                    )
                )
            except KnowflowError as exc:
                log.warning(
                    "Knowflow role lookup failed for knowledge listing; "
                    "falling back to owner-only filtering: %s",
                    exc,
                )
                role_map = {}

        # The deployed Knowflow list endpoint treats the `name` filter inconsistently
        # for manually bound users, so filter the accessible set locally instead.
        datasets = await client.list_datasets(page=1, page_size=1024)
        if context.mode == "managed":
            if role_map:
                datasets = [
                    dataset
                    for dataset in datasets
                    if str(dataset.get("id")) in role_map
                ]
            elif context.knowflow_user_id:
                datasets = [
                    dataset
                    for dataset in datasets
                    if str(dataset.get("created_by") or "").strip()
                    == context.knowflow_user_id
                ]

        effective_user_id = None
        if not context.is_public:
            effective_user_id = _infer_manual_binding_user_id(datasets, context)

        for dataset in datasets:
            item = _normalize_dataset_item(
                dataset,
                role_map=role_map,
                current_user_id=effective_user_id,
                source_mode=context.mode,
            )
            dataset_id = str(item.get("id") or "").strip()
            if not dataset_id:
                continue

            existing = normalized_by_id.get(dataset_id)
            should_replace = existing is None or (
                context.is_public
                and existing.get("meta", {}).get("source_mode") != "public"
            )
            if should_replace:
                normalized_by_id[dataset_id] = item
                contexts_by_id[dataset_id] = context

    normalized = list(normalized_by_id.values())
    normalized.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)

    if query:
        normalized = [item for item in normalized if _matches_knowledge_query(item, query)]

    if view_option == "created":
        normalized = [item for item in normalized if not item.get("is_shared")]
    elif view_option == "shared":
        normalized = [item for item in normalized if item.get("is_shared")]

    total = len(normalized)
    start = max(page - 1, 0) * page_size
    items = normalized[start : start + page_size]
    mode = contexts[0].mode if len(contexts) == 1 else "mixed"
    return {
        "items": items,
        "total": total,
        "mode": mode,
        "_contexts_by_id": contexts_by_id,
    }


async def get_accessible_knowledge_base(
    config: Any, user, knowledge_id: str, *, db=None
) -> Optional[dict[str, Any]]:
    entry = await _get_accessible_knowledge_base_with_context(
        config,
        user,
        knowledge_id,
        db=db,
    )
    if entry is None:
        return None
    return entry[0]


async def _get_accessible_knowledge_base_with_context(
    config: Any, user, knowledge_id: str, *, db=None
) -> Optional[tuple[dict[str, Any], KnowflowContext]]:
    result = await _list_accessible_knowledge_bases_with_context(
        config,
        user,
        page=1,
        page_size=1024,
        db=db,
    )
    contexts_by_id = result.get("_contexts_by_id") or {}
    for item in result.get("items") or []:
        dataset_id = str(item.get("id") or "").strip()
        if dataset_id != str(knowledge_id):
            continue
        context = contexts_by_id.get(dataset_id)
        if context is None:
            return None
        return item, context
    return None


async def list_knowledge_documents(
    config: Any,
    user,
    knowledge_id: str,
    *,
    query: Optional[str] = None,
    order_by: Optional[str] = None,
    direction: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
    db=None,
) -> dict[str, Any]:
    entry = await _get_accessible_knowledge_base_with_context(
        config, user, knowledge_id, db=db
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge base not found",
        )
    dataset, context = entry

    client = KnowflowClient(config, context.api_key)
    document_data = await client.list_documents(
        knowledge_id,
        page=1,
        page_size=1024,
        keywords=query or None,
    )
    documents = document_data.get("docs") or []
    items = [
        _normalize_document_item(
            document,
            current_user_id=context.knowflow_user_id or user.id,
            dataset=dataset,
        )
        for document in documents
    ]

    reverse = direction != "asc"
    if order_by == "name":
        items.sort(key=lambda item: (item.get("meta", {}).get("name") or "").lower(), reverse=reverse)
    elif order_by == "created_at":
        items.sort(key=lambda item: item.get("created_at") or 0, reverse=reverse)
    else:
        items.sort(key=lambda item: item.get("updated_at") or 0, reverse=reverse)

    total = len(items)
    start = max(page - 1, 0) * page_size
    return {"items": items[start : start + page_size], "total": total}


async def search_accessible_documents(
    config: Any,
    user,
    *,
    query: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
    db=None,
) -> dict[str, Any]:
    knowledge_result = await list_accessible_knowledge_bases(
        config,
        user,
        query=None,
        page=1,
        page_size=100,
        db=db,
    )
    items: list[dict[str, Any]] = []
    for knowledge in knowledge_result["items"]:
        documents_result = await list_knowledge_documents(
            config,
            user,
            knowledge["id"],
            query=query,
            page=1,
            page_size=100,
            db=db,
        )
        items.extend(documents_result["items"])

    total = len(items)
    start = max(page - 1, 0) * page_size
    return {"items": items[start : start + page_size], "total": total}


async def retrieve_from_knowledge(
    config: Any,
    user,
    question: str,
    *,
    dataset_ids: Optional[list[str]] = None,
    document_ids: Optional[list[str]] = None,
    page_size: int = 5,
    db=None,
) -> list[dict[str, Any]]:
    knowledge_result = await _list_accessible_knowledge_bases_with_context(
        config,
        user,
        page=1,
        page_size=1024,
        db=db,
    )
    items = knowledge_result.get("items") or []
    contexts_by_id: dict[str, KnowflowContext] = (
        knowledge_result.get("_contexts_by_id") or {}
    )
    if not items or not contexts_by_id:
        return []

    requested_dataset_ids = {
        str(dataset_id).strip() for dataset_id in (dataset_ids or []) if dataset_id
    }
    document_id_list = [
        str(document_id).strip() for document_id in (document_ids or []) if document_id
    ]

    context_dataset_ids: dict[str, list[str]] = {}
    contexts_by_key: dict[str, KnowflowContext] = {}
    for item in items:
        dataset_id = str(item.get("id") or "").strip()
        if not dataset_id:
            continue
        if requested_dataset_ids and dataset_id not in requested_dataset_ids:
            continue

        context = contexts_by_id.get(dataset_id)
        if context is None:
            continue
        context_dataset_ids.setdefault(context.api_key, []).append(dataset_id)
        contexts_by_key[context.api_key] = context

    if requested_dataset_ids and not context_dataset_ids:
        return []

    chunks: list[dict[str, Any]] = []
    seen_chunk_keys: set[str] = set()
    for api_key, scoped_dataset_ids in context_dataset_ids.items():
        context = contexts_by_key[api_key]
        client = KnowflowClient(config, context.api_key)
        try:
            scoped_chunks = await client.retrieve(
                question,
                dataset_ids=scoped_dataset_ids or None,
                document_ids=document_id_list or None,
                page_size=page_size,
            )
        except KnowflowError as exc:
            log.warning("Knowflow retrieval failed for %s context: %s", context.mode, exc)
            continue

        for chunk in scoped_chunks or []:
            document_id = str(chunk.get("document_id") or "").strip()
            content = str(chunk.get("content") or chunk.get("text") or "").strip()
            chunk_key = _first_non_empty_str(
                [
                    str(chunk.get("id") or ""),
                    str(chunk.get("chunk_id") or ""),
                    f"{document_id}:{content[:200]}" if document_id and content else "",
                ]
            )
            if chunk_key and chunk_key in seen_chunk_keys:
                continue
            if chunk_key:
                seen_chunk_keys.add(chunk_key)
            chunks.append(chunk)

    return chunks
