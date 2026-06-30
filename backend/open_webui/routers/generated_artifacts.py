import hashlib
import hmac
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from open_webui.constants import ERROR_MESSAGES
from open_webui.internal.db import get_session
from open_webui.models.chats import Chats
from open_webui.models.generated_artifacts import (
    ACTIVE_PUBLICATION_STATES,
    ARTIFACT_STATE_DELETED,
    ARTIFACT_STATE_FAILED,
    ARTIFACT_STATE_PUBLISHED,
    ARTIFACT_STATE_UPLOADING,
    GeneratedArtifactFailForm,
    GeneratedArtifactFinalizeForm,
    GeneratedArtifactModel,
    GeneratedArtifactPatchForm,
    GeneratedArtifactReconcileForm,
    GeneratedArtifactReserveForm,
    GeneratedArtifacts,
    compute_reservation_fingerprint,
    create_publication_token,
    derive_publication_token,
    now_ts,
    verify_publication_token,
)
from open_webui.storage.provider import (
    Storage,
    StorageFileNotFoundError,
    cleanup_ephemeral_storage_file,
    is_ephemeral_storage_file,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.artifact_publication_scope import (
    verify_artifact_publication_scope,
)

log = logging.getLogger(__name__)

router = APIRouter()

_PUBLISHED_READBACK_TOKEN_SENTINEL = "__already_published__"

GENERATED_ARTIFACTS_PREFIX = os.environ.get(
    "GENERATED_ARTIFACTS_STORAGE_PREFIX",
    "generated-artifacts",
).strip("/")


class GeneratedArtifactResponse(BaseModel):
    artifact_id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    content_sha256: Optional[str] = None
    etag: Optional[str] = None
    publication_state: str
    failure_reason: Optional[str] = None
    owner_chat_id: Optional[str] = None
    owner_message_id: Optional[str] = None
    reference_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    url: str
    created_at: int
    updated_at: int
    published_at: Optional[int] = None
    deleted_at: Optional[int] = None


class GeneratedArtifactReserveResponse(BaseModel):
    artifact: GeneratedArtifactResponse
    publication_session_id: str
    publication_token: str
    s3_key: str
    storage_provider: str
    upload_capability: dict[str, Any]
    upload_expires_at: Optional[int] = None


class GeneratedArtifactReconcileResponse(BaseModel):
    failed_count: int


def _artifact_url(artifact_id: str) -> str:
    return f"/api/v1/generated-artifacts/{artifact_id}/content"


def _artifact_response(artifact: GeneratedArtifactModel) -> GeneratedArtifactResponse:
    return GeneratedArtifactResponse(
        artifact_id=artifact.artifact_id,
        filename=artifact.filename,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        content_sha256=artifact.content_sha256,
        etag=artifact.etag,
        publication_state=artifact.publication_state,
        failure_reason=artifact.failure_reason,
        owner_chat_id=artifact.owner_chat_id,
        owner_message_id=artifact.owner_message_id,
        reference_id=artifact.reference_id,
        tool_call_id=artifact.tool_call_id,
        url=_artifact_url(artifact.artifact_id),
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        published_at=artifact.published_at,
        deleted_at=artifact.deleted_at,
    )


def _safe_filename(filename: str) -> str:
    safe_name = os.path.basename(str(filename or "").strip())
    safe_name = re.sub(r"[\x00-\x1f\x7f]+", "", safe_name).strip()
    return safe_name or "artifact"


def _build_relative_object_key(user_id: str, artifact_id: str, filename: str) -> str:
    safe_user_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", user_id or "unknown")
    safe_filename = _safe_filename(filename)
    return "/".join(
        part.strip("/")
        for part in (
            GENERATED_ARTIFACTS_PREFIX,
            "users",
            safe_user_id,
            artifact_id,
            safe_filename,
        )
        if part and part.strip("/")
    )


def _storage_provider_name() -> str:
    return Storage.__class__.__name__.replace("StorageProvider", "").lower() or "unknown"


def _storage_bucket_name() -> Optional[str]:
    return getattr(Storage, "bucket_name", None)


def _get_internal_api_key(request: Request) -> str:
    config_key = str(
        getattr(
            getattr(request.app.state, "config", object()),
            "GENERATED_ARTIFACTS_SERVICE_API_KEY",
            "",
        )
        or ""
    ).strip()
    return config_key or os.environ.get("GENERATED_ARTIFACTS_SERVICE_API_KEY", "").strip()


def _extract_internal_token(request: Request) -> str:
    for header_name in (
        "x-generated-artifacts-api-key",
        "x-internal-api-key",
        "x-api-key",
    ):
        value = request.headers.get(header_name)
        if value:
            return value.strip()

    auth_header = request.headers.get("authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return ""


def require_internal_artifact_key(request: Request) -> None:
    expected = _get_internal_api_key(request)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ERROR_MESSAGES.DEFAULT(
                "Generated artifact internal API key is not configured"
            ),
        )

    supplied = _extract_internal_token(request)
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


def _assert_artifact_access(artifact: GeneratedArtifactModel, user) -> None:
    if user.role == "admin" or artifact.user_id == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ERROR_MESSAGES.NOT_FOUND,
    )


def _resolve_user_from_scope_token(
    scope_token: str,
    *,
    expected_operation: str = "reserve",
) -> Optional[Any]:
    """Resolve user context from a scoped artifact publication token.

    Returns a minimal user-like object with id, role, name, email attributes
    if the token is valid, or None if resolution fails.
    """
    from open_webui.models.users import Users

    try:
        payload = verify_artifact_publication_scope(
            scope_token,
            required_operation=expected_operation,
        )
    except ValueError:
        return None

    user_id = payload.get("uid")
    if not user_id or not isinstance(user_id, str):
        return None

    user = Users.get_user_by_id(user_id)
    if user is None:
        return None

    # Return a lightweight proxy that satisfies the reservation flow
    # without granting general API access.
    return user


def _get_chat_message_from_history(chat_data: dict, message_id: str) -> Optional[dict]:
    history = (chat_data or {}).get("history") if isinstance(chat_data, dict) else {}
    messages = history.get("messages") if isinstance(history, dict) else None

    if isinstance(messages, dict):
        message = messages.get(message_id)
        return message if isinstance(message, dict) else None

    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and str(message.get("id") or "") == message_id:
                return message

    return None


def _assert_reservation_scope(
    form_data: GeneratedArtifactReserveForm,
    user,
    db: Session,
) -> None:
    if form_data.owner_message_id and not form_data.owner_chat_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "owner_message_id requires an owner_chat_id scope"
            ),
        )
    if form_data.tool_call_id and not form_data.owner_message_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "tool_call_id requires an owner_message_id scope"
            ),
        )
    if not form_data.owner_chat_id:
        return

    chat = (
        Chats.get_chat_by_id(form_data.owner_chat_id, db=db)
        if user.role == "admin"
        else Chats.get_chat_by_id_and_user_id(form_data.owner_chat_id, user.id, db=db)
    )
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if form_data.owner_message_id:
        if not _get_chat_message_from_history(
            chat.chat,
            form_data.owner_message_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

    if form_data.tool_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "tool_call_id scope is not supported until canonical tool-call state is available"
            ),
        )


def _get_artifact_or_404(
    artifact_id: str, db: Session
) -> GeneratedArtifactModel:
    artifact = GeneratedArtifacts.get_artifact_by_id(artifact_id, db=db)
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return artifact


def _assert_publication_token(
    artifact: GeneratedArtifactModel, publication_token: str, db: Session
) -> None:
    db_row = GeneratedArtifacts.get_artifact_row_by_id(artifact.artifact_id, db)
    token_hash = db_row.publication_token_hash if db_row else ""
    if not verify_publication_token(token_hash, publication_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


def _build_upload_capability(
    *,
    storage_uri: str,
    s3_key: str,
    content_type: Optional[str],
    content_sha256: Optional[str],
    expires_in: int,
) -> dict[str, Any]:
    metadata = {"sha256": content_sha256} if content_sha256 else {}
    presigned = Storage.create_presigned_upload_url(
        storage_uri,
        content_type=content_type,
        metadata=metadata,
        expires_in=expires_in,
    )
    if presigned:
        return {
            "type": "presigned_put",
            "storage_uri": storage_uri,
            "s3_key": s3_key,
            **presigned,
        }

    artifact_key_prefix = s3_key.rsplit("/", 1)[0].rstrip("/")
    required_key_prefix = f"{artifact_key_prefix}/" if artifact_key_prefix else s3_key
    return {
        "type": "storage_client",
        "storage_uri": storage_uri,
        "s3_key": s3_key,
        "required_key_prefix": required_key_prefix,
        "credential_policy": "external_prefix_limited_credentials_required",
    }


def _normalize_etag(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip().strip('"')
    return normalized or None


def _fail_artifact(
    artifact_id: str,
    reason: str,
    db: Session,
) -> None:
    GeneratedArtifacts.mark_failed(artifact_id, failure_reason=reason, db=db)


def _assert_publication_not_expired(
    artifact: GeneratedArtifactModel,
    db: Session,
) -> None:
    if artifact.publication_state not in ACTIVE_PUBLICATION_STATES:
        return
    if not artifact.upload_expires_at or artifact.upload_expires_at >= now_ts():
        return

    reason = "publication_session_expired"
    _fail_artifact(artifact.artifact_id, reason, db)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ERROR_MESSAGES.DEFAULT(reason),
    )


def _verify_uploaded_object(
    artifact: GeneratedArtifactModel,
    form_data: GeneratedArtifactFinalizeForm | GeneratedArtifactPatchForm,
    db: Session,
) -> dict[str, Any]:
    try:
        metadata = Storage.get_file_metadata(artifact.storage_uri)
    except Exception as exc:
        reason = f"object_metadata_unavailable: {exc}"
        _fail_artifact(artifact.artifact_id, reason, db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(reason),
        ) from exc

    actual_size = metadata.get("size_bytes")
    expected_size = (
        form_data.size_bytes if form_data.size_bytes is not None else artifact.size_bytes
    )
    if expected_size is not None and actual_size != expected_size:
        reason = "uploaded_object_size_mismatch"
        _fail_artifact(artifact.artifact_id, reason, db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(reason),
        )

    actual_content_type = metadata.get("content_type")
    expected_content_type = form_data.content_type or artifact.content_type
    if (
        expected_content_type
        and actual_content_type
        and actual_content_type.lower() != expected_content_type.lower()
    ):
        reason = "uploaded_object_content_type_mismatch"
        _fail_artifact(artifact.artifact_id, reason, db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(reason),
        )

    expected_sha256 = (
        str(form_data.content_sha256 or artifact.content_sha256 or "").lower() or None
    )
    actual_sha256 = None
    if expected_sha256:
        # Compute SHA-256 from the actual stored object bytes rather than
        # trusting upload metadata (x-amz-meta-sha256), which the uploader
        # controls and could set to an arbitrary value.
        local_path = None
        try:
            local_path = Storage.get_file(artifact.storage_uri)
            hasher = hashlib.sha256()
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    hasher.update(chunk)
            actual_sha256 = hasher.hexdigest()
        except Exception as exc:
            reason = f"object_sha256_computation_failed: {exc}"
            _fail_artifact(artifact.artifact_id, reason, db)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ERROR_MESSAGES.DEFAULT(reason),
            ) from exc
        finally:
            if local_path:
                cleanup_ephemeral_storage_file(local_path)

        if actual_sha256 != expected_sha256:
            reason = "uploaded_object_sha256_mismatch"
            _fail_artifact(artifact.artifact_id, reason, db)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ERROR_MESSAGES.DEFAULT(reason),
            )

    actual_etag = _normalize_etag(metadata.get("etag"))
    expected_etag = _normalize_etag(form_data.etag)
    if expected_etag and actual_etag != expected_etag:
        reason = "uploaded_object_etag_mismatch"
        _fail_artifact(artifact.artifact_id, reason, db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(reason),
        )

    if not expected_sha256 and not expected_etag and not actual_etag:
        reason = "uploaded_object_hash_or_etag_missing"
        _fail_artifact(artifact.artifact_id, reason, db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(reason),
        )

    return {
        "size_bytes": actual_size,
        "content_type": actual_content_type or expected_content_type,
        "content_sha256": actual_sha256,
        "etag": actual_etag,
    }


def _file_response(
    file_path: Path, *, headers: Optional[dict] = None, media_type: Optional[str] = None
):
    background = None
    if is_ephemeral_storage_file(file_path):
        background = BackgroundTask(cleanup_ephemeral_storage_file, str(file_path))

    return FileResponse(
        file_path,
        headers=headers,
        media_type=media_type,
        background=background,
    )


def _delete_artifact_storage_or_409(
    artifact: GeneratedArtifactModel, *, allow_missing: bool = False
) -> None:
    try:
        Storage.get_file_metadata(artifact.storage_uri)
    except StorageFileNotFoundError as exc:
        if allow_missing:
            log.info(
                "Generated artifact storage object absent before delete for %s; "
                "metadata deletion may proceed",
                artifact.artifact_id,
            )
            return

        log.exception(
            "Generated artifact storage object missing before delete for %s: %s",
            artifact.artifact_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(
                "Generated artifact storage object is missing; artifact metadata was not deleted"
            ),
        ) from exc
    except Exception as exc:
        log.exception(
            "Generated artifact storage pre-delete check failed for %s: %s",
            artifact.artifact_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(
                "Generated artifact storage cleanup could not verify the object; artifact metadata was not deleted"
            ),
        ) from exc

    try:
        Storage.delete_file(artifact.storage_uri)
    except Exception as exc:
        log.exception(
            "Generated artifact storage cleanup failed for %s: %s",
            artifact.artifact_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(
                "Generated artifact storage cleanup failed; artifact metadata was not deleted"
            ),
        ) from exc

    try:
        Storage.get_file_metadata(artifact.storage_uri)
    except StorageFileNotFoundError:
        return
    except Exception as exc:
        log.exception(
            "Generated artifact storage post-delete verification failed for %s: %s",
            artifact.artifact_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT(
                "Generated artifact storage cleanup could not verify removal; artifact metadata was not deleted"
            ),
        ) from exc

    log.error(
        "Generated artifact storage cleanup left object in place for %s",
        artifact.artifact_id,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ERROR_MESSAGES.DEFAULT(
            "Generated artifact storage cleanup did not remove the object; artifact metadata was not deleted"
        ),
    )


@router.post("/", response_model=GeneratedArtifactReserveResponse)
async def reserve_generated_artifact(
    request: Request,
    form_data: GeneratedArtifactReserveForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    require_internal_artifact_key(request)
    _assert_reservation_scope(form_data, user, db)

    artifact_id = str(uuid.uuid4())
    publication_session_id = str(uuid.uuid4())
    publication_token, token_hash = create_publication_token()
    filename = _safe_filename(form_data.filename)
    normalized_form = form_data.model_copy(update={"filename": filename})

    relative_key = _build_relative_object_key(user.id, artifact_id, filename)
    s3_key = Storage.build_object_key(relative_key)
    storage_uri = Storage.build_file_uri(s3_key)

    upload_capability = _build_upload_capability(
        storage_uri=storage_uri,
        s3_key=s3_key,
        content_type=normalized_form.content_type,
        content_sha256=normalized_form.content_sha256,
        expires_in=normalized_form.upload_expires_in,
    )

    artifact = GeneratedArtifacts.insert_reserved_artifact(
        user.id,
        normalized_form,
        storage_provider=_storage_provider_name(),
        storage_bucket=_storage_bucket_name(),
        s3_key=s3_key,
        storage_uri=storage_uri,
        publication_token_hash=token_hash,
        artifact_id=artifact_id,
        publication_session_id=publication_session_id,
        db=db,
    )
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error reserving generated artifact"),
        )

    return GeneratedArtifactReserveResponse(
        artifact=_artifact_response(artifact),
        publication_session_id=artifact.publication_session_id,
        publication_token=publication_token,
        s3_key=artifact.s3_key,
        storage_provider=artifact.storage_provider,
        upload_capability=upload_capability,
        upload_expires_at=artifact.upload_expires_at,
    )


@router.post("/reserve-with-scope", response_model=GeneratedArtifactReserveResponse)
async def reserve_generated_artifact_with_scope(
    request: Request,
    form_data: GeneratedArtifactReserveForm,
    db: Session = Depends(get_session),
):
    """Agent-originated reservation using scoped publication context.

    Requires:
    - Internal service API key (require_internal_artifact_key)
    - Valid scope_token with jti in form data (provides user/chat/message context)
    - reservation_idempotency_key for replay protection

    Does NOT require user JWT. The scope token provides the authorization
    context that would otherwise come from get_verified_user.
    """
    require_internal_artifact_key(request)

    if not form_data.scope_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "scope_token is required for agent-originated artifact reservation"
            ),
        )

    if not form_data.reservation_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "reservation_idempotency_key is required for scoped reservation"
            ),
        )

    user = _resolve_user_from_scope_token(
        form_data.scope_token,
        expected_operation="reserve",
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.DEFAULT(
                "Invalid or expired artifact publication scope token"
            ),
        )

    # Validate scope token claims against form data
    try:
        scope_payload = verify_artifact_publication_scope(
            form_data.scope_token,
            required_operation="reserve",
            expected_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.DEFAULT(str(exc)),
        ) from exc

    # Extract jti from scope token — required for idempotency
    scope_token_jti = scope_payload.get("jti")
    if not scope_token_jti or not isinstance(scope_token_jti, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(
                "scope token must contain jti for idempotent scoped reservation"
            ),
        )

    # Use scope token claims to enforce chat/message scope
    scope_chat_id = scope_payload.get("cid")
    scope_message_id = scope_payload.get("mid")

    # Derive owner scope from the token. Reject form-provided values that
    # conflict with or go beyond the token's claims.
    if form_data.owner_chat_id:
        if not scope_chat_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.DEFAULT(
                    "scope token does not carry chat_id; form-provided owner_chat_id is not permitted"
                ),
            )
        if form_data.owner_chat_id != scope_chat_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.DEFAULT(
                    "owner_chat_id does not match artifact publication scope"
                ),
            )
    if form_data.owner_message_id:
        if not scope_message_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.DEFAULT(
                    "scope token does not carry message_id; form-provided owner_message_id is not permitted"
                ),
            )
        if form_data.owner_message_id != scope_message_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.DEFAULT(
                    "owner_message_id does not match artifact publication scope"
                ),
            )

    # Derive owner scope from token claims (token is authoritative)
    if scope_chat_id and not form_data.owner_chat_id:
        form_data = form_data.model_copy(update={"owner_chat_id": scope_chat_id})
    if scope_message_id and not form_data.owner_message_id:
        form_data = form_data.model_copy(update={"owner_message_id": scope_message_id})

    _assert_reservation_scope(form_data, user, db)

    filename = _safe_filename(form_data.filename)
    normalized_form = form_data.model_copy(update={"filename": filename})

    # Compute fingerprint for duplicate detection
    fingerprint = compute_reservation_fingerprint(
        normalized_form.filename,
        normalized_form.content_type,
        normalized_form.size_bytes,
        normalized_form.content_sha256,
    )

    # --- Idempotency check: look up existing artifact by (jti, key) ---
    existing = GeneratedArtifacts.get_artifact_by_idempotency_key(
        scope_token_jti,
        normalized_form.reservation_idempotency_key,
        db=db,
    )

    if existing:
        # Idempotency key already used — check fingerprint, then state.

        if existing.reservation_fingerprint and existing.reservation_fingerprint != fingerprint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ERROR_MESSAGES.DEFAULT(
                    "reservation_idempotency_key conflict: same key used with different artifact content"
                ),
            )

        if existing.publication_state == ARTIFACT_STATE_PUBLISHED:
            return GeneratedArtifactReserveResponse(
                artifact=_artifact_response(existing),
                publication_session_id=existing.publication_session_id,
                publication_token=_PUBLISHED_READBACK_TOKEN_SENTINEL,
                s3_key=existing.s3_key,
                storage_provider=existing.storage_provider,
                upload_capability={},
                upload_expires_at=existing.upload_expires_at,
            )

        if existing.publication_state in (ARTIFACT_STATE_FAILED, ARTIFACT_STATE_DELETED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ERROR_MESSAGES.DEFAULT(
                    "reservation_idempotency_key already consumed by terminal artifact state"
                ),
            )

        pub_token, _ = derive_publication_token(
            existing.artifact_id,
            scope_token_jti,
            normalized_form.reservation_idempotency_key,
        )

        upload_capability = _build_upload_capability(
            storage_uri=existing.storage_uri,
            s3_key=existing.s3_key,
            content_type=normalized_form.content_type,
            content_sha256=normalized_form.content_sha256,
            expires_in=normalized_form.upload_expires_in,
        )

        return GeneratedArtifactReserveResponse(
            artifact=_artifact_response(existing),
            publication_session_id=existing.publication_session_id,
            publication_token=pub_token,
            s3_key=existing.s3_key,
            storage_provider=existing.storage_provider,
            upload_capability=upload_capability,
            upload_expires_at=existing.upload_expires_at,
        )

    # --- New reservation with idempotency fields ---
    artifact_id = str(uuid.uuid4())
    publication_session_id = str(uuid.uuid4())

    pub_token, token_hash = derive_publication_token(
        artifact_id, scope_token_jti, normalized_form.reservation_idempotency_key,
    )

    relative_key = _build_relative_object_key(user.id, artifact_id, filename)
    s3_key = Storage.build_object_key(relative_key)
    storage_uri = Storage.build_file_uri(s3_key)

    upload_capability = _build_upload_capability(
        storage_uri=storage_uri,
        s3_key=s3_key,
        content_type=normalized_form.content_type,
        content_sha256=normalized_form.content_sha256,
        expires_in=normalized_form.upload_expires_in,
    )

    artifact = GeneratedArtifacts.insert_reserved_artifact(
        user.id,
        normalized_form,
        storage_provider=_storage_provider_name(),
        storage_bucket=_storage_bucket_name(),
        s3_key=s3_key,
        storage_uri=storage_uri,
        publication_token_hash=token_hash,
        artifact_id=artifact_id,
        publication_session_id=publication_session_id,
        scope_token_jti=scope_token_jti,
        reservation_idempotency_key=normalized_form.reservation_idempotency_key,
        reservation_fingerprint=fingerprint,
        db=db,
    )
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error reserving generated artifact"),
        )

    return GeneratedArtifactReserveResponse(
        artifact=_artifact_response(artifact),
        publication_session_id=artifact.publication_session_id,
        publication_token=pub_token,
        s3_key=artifact.s3_key,
        storage_provider=artifact.storage_provider,
        upload_capability=upload_capability,
        upload_expires_at=artifact.upload_expires_at,
    )


@router.post("/reconcile", response_model=GeneratedArtifactReconcileResponse)
async def reconcile_generated_artifacts(
    request: Request,
    form_data: GeneratedArtifactReconcileForm,
    db: Session = Depends(get_session),
):
    require_internal_artifact_key(request)
    stale_before = now_ts() - form_data.stale_after_seconds
    failed_count = GeneratedArtifacts.mark_stale_publications_failed(
        stale_before=stale_before,
        limit=form_data.limit,
        db=db,
    )
    return GeneratedArtifactReconcileResponse(failed_count=failed_count)


@router.get("/{artifact_id}", response_model=GeneratedArtifactResponse)
async def get_generated_artifact(
    artifact_id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_artifact_access(artifact, user)
    return _artifact_response(artifact)


@router.patch("/{artifact_id}", response_model=GeneratedArtifactResponse)
async def patch_generated_artifact(
    artifact_id: str,
    form_data: GeneratedArtifactPatchForm,
    request: Request,
    db: Session = Depends(get_session),
):
    require_internal_artifact_key(request)
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_publication_token(artifact, form_data.publication_token, db)

    if form_data.publication_state == ARTIFACT_STATE_UPLOADING:
        _assert_publication_not_expired(artifact, db)
        updated = GeneratedArtifacts.mark_uploading(artifact_id, db=db)
    elif form_data.publication_state == ARTIFACT_STATE_PUBLISHED:
        if artifact.publication_state == ARTIFACT_STATE_PUBLISHED:
            return _artifact_response(artifact)
        _assert_publication_not_expired(artifact, db)
        verified = _verify_uploaded_object(artifact, form_data, db)
        updated = GeneratedArtifacts.mark_published(
            artifact_id,
            size_bytes=verified["size_bytes"],
            content_type=verified["content_type"],
            content_sha256=verified["content_sha256"],
            etag=verified["etag"],
            db=db,
        )
    elif form_data.publication_state == ARTIFACT_STATE_FAILED:
        updated = GeneratedArtifacts.mark_failed(
            artifact_id,
            failure_reason=form_data.failure_reason or "publication_failed",
            db=db,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Invalid publication state transition"),
        )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT("Invalid artifact publication transition"),
        )
    return _artifact_response(updated)


@router.post("/{artifact_id}/uploading", response_model=GeneratedArtifactResponse)
async def mark_generated_artifact_uploading(
    artifact_id: str,
    form_data: GeneratedArtifactFinalizeForm,
    request: Request,
    db: Session = Depends(get_session),
):
    require_internal_artifact_key(request)
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_publication_token(artifact, form_data.publication_token, db)
    _assert_publication_not_expired(artifact, db)
    updated = GeneratedArtifacts.mark_uploading(artifact_id, db=db)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT("Invalid artifact publication transition"),
        )
    return _artifact_response(updated)


@router.post("/{artifact_id}/finalize", response_model=GeneratedArtifactResponse)
async def finalize_generated_artifact(
    artifact_id: str,
    form_data: GeneratedArtifactFinalizeForm,
    request: Request,
    db: Session = Depends(get_session),
):
    # Finalize uses publication_token (returned by reservation) as its
    # authorization capability. No scope token needed — the publication_token
    # is per-artifact and proves the caller reserved this specific artifact.
    require_internal_artifact_key(request)
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_publication_token(artifact, form_data.publication_token, db)
    if artifact.publication_state == ARTIFACT_STATE_PUBLISHED:
        return _artifact_response(artifact)
    _assert_publication_not_expired(artifact, db)
    verified = _verify_uploaded_object(artifact, form_data, db)
    updated = GeneratedArtifacts.mark_published(
        artifact_id,
        size_bytes=verified["size_bytes"],
        content_type=verified["content_type"],
        content_sha256=verified["content_sha256"],
        etag=verified["etag"],
        db=db,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT("Invalid artifact publication transition"),
        )
    return _artifact_response(updated)


@router.post("/{artifact_id}/fail", response_model=GeneratedArtifactResponse)
async def fail_generated_artifact(
    artifact_id: str,
    form_data: GeneratedArtifactFailForm,
    request: Request,
    db: Session = Depends(get_session),
):
    require_internal_artifact_key(request)
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_publication_token(artifact, form_data.publication_token, db)
    updated = GeneratedArtifacts.mark_failed(
        artifact_id,
        failure_reason=form_data.failure_reason,
        db=db,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ERROR_MESSAGES.DEFAULT("Invalid artifact publication transition"),
        )
    return _artifact_response(updated)


@router.get("/{artifact_id}/content")
async def get_generated_artifact_content(
    artifact_id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_artifact_access(artifact, user)
    if artifact.publication_state != ARTIFACT_STATE_PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    file_path = None
    try:
        file_path = Path(Storage.get_file(artifact.storage_uri))
        if not file_path.is_file():
            cleanup_ephemeral_storage_file(file_path)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

        encoded_filename = quote(artifact.filename)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
        return _file_response(
            file_path,
            headers=headers,
            media_type=artifact.content_type,
        )
    except HTTPException:
        raise
    except Exception as exc:
        cleanup_ephemeral_storage_file(file_path)
        log.exception("Error getting generated artifact content: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error getting generated artifact content"),
        ) from exc


@router.delete("/{artifact_id}", response_model=GeneratedArtifactResponse)
async def delete_generated_artifact(
    artifact_id: str,
    cleanup_storage: bool = True,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    artifact = _get_artifact_or_404(artifact_id, db)
    _assert_artifact_access(artifact, user)
    storage_backed_state = (
        artifact.publication_state in ACTIVE_PUBLICATION_STATES
        or artifact.publication_state == ARTIFACT_STATE_FAILED
        or artifact.publication_state == ARTIFACT_STATE_PUBLISHED
    )
    if storage_backed_state:
        if not cleanup_storage:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ERROR_MESSAGES.DEFAULT(
                    "Generated artifact storage cleanup is required before deleting active, failed, or published artifact metadata"
                ),
            )
        _delete_artifact_storage_or_409(
            artifact,
            allow_missing=artifact.publication_state != ARTIFACT_STATE_PUBLISHED,
        )

    updated = GeneratedArtifacts.soft_delete_artifact(artifact_id, db=db)
    if updated and updated.publication_state == ARTIFACT_STATE_DELETED:
        return _artifact_response(updated)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ERROR_MESSAGES.DEFAULT("Error deleting generated artifact"),
    )
