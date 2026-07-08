import hashlib
import hmac
import logging
import secrets
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Column, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db_context

log = logging.getLogger(__name__)

ARTIFACT_STATE_RESERVED = "reserved"
ARTIFACT_STATE_DRAFT = "draft"
ARTIFACT_STATE_UPLOADING = "uploading"
ARTIFACT_STATE_PUBLISHED = "published"
ARTIFACT_STATE_FAILED = "failed"
ARTIFACT_STATE_DELETED = "deleted"

ACTIVE_PUBLICATION_STATES = {
    ARTIFACT_STATE_RESERVED,
    ARTIFACT_STATE_DRAFT,
    ARTIFACT_STATE_UPLOADING,
}

TERMINAL_PUBLICATION_STATES = {
    ARTIFACT_STATE_PUBLISHED,
    ARTIFACT_STATE_FAILED,
    ARTIFACT_STATE_DELETED,
}


def now_ts() -> int:
    return int(time.time())


def create_publication_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_publication_token(token)


def hash_publication_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def verify_publication_token(token_hash: str, token: str) -> bool:
    if not token_hash or not token:
        return False
    return hmac.compare_digest(token_hash, hash_publication_token(token))


_IDEMPOTENCY_TOKEN_PURPOSE = b"generated_artifact_publication_token_derivation"


def _idempotency_token_signing_key() -> bytes:
    """Purpose-separated key for deterministic publication token derivation.

    Never logged or stored in plaintext. Derived from WEBUI_SECRET_KEY
    with a dedicated salt to avoid cross-purpose key reuse.
    """
    from open_webui.env import WEBUI_SECRET_KEY
    return hmac.new(
        WEBUI_SECRET_KEY.encode("utf-8"),
        _IDEMPOTENCY_TOKEN_PURPOSE,
        hashlib.sha256,
    ).digest()


def derive_publication_token(
    artifact_id: str,
    scope_token_jti: str,
    reservation_idempotency_key: str,
) -> tuple[str, str]:
    """Deterministic reissuable publication token for idempotent readback.

    Same inputs produce the same token. Returns (plaintext_token, token_hash).
    The plaintext is returned to the caller but NEVER stored or logged.
    Only the hash is persisted in publication_token_hash.
    """
    msg = f"{artifact_id}:{scope_token_jti}:{reservation_idempotency_key}".encode()
    token = hmac.new(
        _idempotency_token_signing_key(), msg, hashlib.sha256
    ).hexdigest()
    token_hash = hash_publication_token(token)
    return token, token_hash


def compute_reservation_fingerprint(
    filename: str,
    content_type: Optional[str],
    size_bytes: Optional[int],
    content_sha256: Optional[str],
) -> str:
    """Normalized fingerprint for idempotent duplicate detection.

    Same (filename, content_type, size, sha256) → same fingerprint.
    """
    parts = [
        filename.strip().lower(),
        (content_type or "").strip().lower(),
        str(size_bytes) if size_bytes is not None else "",
        (content_sha256 or "").strip().lower(),
    ]
    return "|".join(parts)


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifacts"

    artifact_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)

    storage_provider = Column(String, nullable=False)
    storage_bucket = Column(Text, nullable=True)
    s3_key = Column(Text, nullable=False)
    storage_uri = Column(Text, nullable=False)

    filename = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    content_sha256 = Column(Text, nullable=True)
    etag = Column(Text, nullable=True)

    publication_state = Column(String, nullable=False)
    failure_reason = Column(Text, nullable=True)

    owner_chat_id = Column(String, nullable=True, index=True)
    owner_message_id = Column(String, nullable=True, index=True)
    reference_id = Column(String, nullable=True, index=True)
    tool_call_id = Column(String, nullable=True, index=True)

    publication_session_id = Column(String, nullable=False)
    publication_token_hash = Column(Text, nullable=False)
    upload_expires_at = Column(BigInteger, nullable=True)

    scope_token_jti = Column(String, nullable=True, index=True)
    reservation_idempotency_key = Column(Text, nullable=True)
    reservation_fingerprint = Column(Text, nullable=True)

    data = Column(JSON, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    published_at = Column(BigInteger, nullable=True)
    deleted_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("s3_key", name="uq_generated_artifacts_s3_key"),
        UniqueConstraint(
            "publication_session_id",
            name="uq_generated_artifacts_publication_session_id",
        ),
        Index(
            "idx_generated_artifacts_idempotency",
            "scope_token_jti",
            "reservation_idempotency_key",
            unique=True,
        ),
        Index(
            "idx_generated_artifacts_owner_message",
            "owner_chat_id",
            "owner_message_id",
        ),
        Index(
            "idx_generated_artifacts_user_state",
            "user_id",
            "publication_state",
        ),
    )


class GeneratedArtifactModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    user_id: str

    storage_provider: str
    storage_bucket: Optional[str] = None
    s3_key: str
    storage_uri: str

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

    publication_session_id: str
    upload_expires_at: Optional[int] = None

    scope_token_jti: Optional[str] = None
    reservation_idempotency_key: Optional[str] = None
    reservation_fingerprint: Optional[str] = None

    data: Optional[dict] = None

    created_at: int
    updated_at: int
    published_at: Optional[int] = None
    deleted_at: Optional[int] = None


class GeneratedArtifactReserveForm(BaseModel):
    filename: str = Field(..., min_length=1)
    content_type: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    content_sha256: Optional[str] = None

    owner_chat_id: Optional[str] = None
    owner_message_id: Optional[str] = None
    reference_id: Optional[str] = None
    tool_call_id: Optional[str] = None

    data: dict = Field(default_factory=dict)
    upload_expires_in: int = Field(default=900, ge=60, le=3600)

    # Scoped artifact publication context: minted by Open WebUI, forwarded
    # through bridge into LangGraph state, and presented by agent runtime.
    # When present and valid, it provides user/chat/message scope for
    # agent-originated calls that lack a user JWT.
    scope_token: Optional[str] = None

    # Required for /reserve-with-scope. The agent supplies a stable key
    # derived from the artifact's content identity so retries within the
    # same scope token (jti) are idempotent. Ignored for direct JWT reserve.
    reservation_idempotency_key: Optional[str] = None


class GeneratedArtifactFinalizeForm(BaseModel):
    publication_token: str = Field(..., min_length=1)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    content_type: Optional[str] = None
    content_sha256: Optional[str] = None
    etag: Optional[str] = None


class GeneratedArtifactFailForm(BaseModel):
    publication_token: str = Field(..., min_length=1)
    failure_reason: str = Field(default="publication_failed", min_length=1)


class GeneratedArtifactPatchForm(BaseModel):
    publication_token: str = Field(..., min_length=1)
    publication_state: str
    failure_reason: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    content_type: Optional[str] = None
    content_sha256: Optional[str] = None
    etag: Optional[str] = None


class GeneratedArtifactReconcileForm(BaseModel):
    stale_after_seconds: int = Field(default=3600, ge=60)
    limit: int = Field(default=100, ge=1, le=1000)


class GeneratedArtifactsTable:
    def insert_reserved_artifact(
        self,
        user_id: str,
        form_data: GeneratedArtifactReserveForm,
        *,
        storage_provider: str,
        storage_bucket: Optional[str],
        s3_key: str,
        storage_uri: str,
        publication_token_hash: str,
        artifact_id: Optional[str] = None,
        publication_session_id: Optional[str] = None,
        scope_token_jti: Optional[str] = None,
        reservation_idempotency_key: Optional[str] = None,
        reservation_fingerprint: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> Optional[GeneratedArtifactModel]:
        timestamp = now_ts()
        artifact = GeneratedArtifact(
            artifact_id=artifact_id or str(uuid.uuid4()),
            user_id=user_id,
            storage_provider=storage_provider,
            storage_bucket=storage_bucket,
            s3_key=s3_key,
            storage_uri=storage_uri,
            filename=form_data.filename,
            content_type=form_data.content_type,
            size_bytes=form_data.size_bytes,
            content_sha256=form_data.content_sha256,
            publication_state=ARTIFACT_STATE_RESERVED,
            owner_chat_id=form_data.owner_chat_id,
            owner_message_id=form_data.owner_message_id,
            reference_id=form_data.reference_id,
            tool_call_id=form_data.tool_call_id,
            publication_session_id=publication_session_id or str(uuid.uuid4()),
            publication_token_hash=publication_token_hash,
            upload_expires_at=timestamp + form_data.upload_expires_in,
            scope_token_jti=scope_token_jti,
            reservation_idempotency_key=reservation_idempotency_key,
            reservation_fingerprint=reservation_fingerprint,
            data=form_data.data or {},
            created_at=timestamp,
            updated_at=timestamp,
        )

        with get_db_context(db) as db:
            try:
                db.add(artifact)
                db.commit()
                db.refresh(artifact)
                return GeneratedArtifactModel.model_validate(artifact)
            except Exception as e:
                db.rollback()
                log.exception("Error inserting generated artifact: %s", e)
                return None

    def get_artifact_by_idempotency_key(
        self,
        scope_token_jti: str,
        reservation_idempotency_key: str,
        db: Optional[Session] = None,
    ) -> Optional[GeneratedArtifactModel]:
        with get_db_context(db) as db:
            artifact = (
                db.query(GeneratedArtifact)
                .filter(
                    GeneratedArtifact.scope_token_jti == scope_token_jti,
                    GeneratedArtifact.reservation_idempotency_key == reservation_idempotency_key,
                )
                .first()
            )
            if not artifact:
                return None
            return GeneratedArtifactModel.model_validate(artifact)

    def get_artifact_by_id(
        self, artifact_id: str, db: Optional[Session] = None
    ) -> Optional[GeneratedArtifactModel]:
        with get_db_context(db) as db:
            artifact = db.get(GeneratedArtifact, artifact_id)
            if not artifact:
                return None
            return GeneratedArtifactModel.model_validate(artifact)

    def get_artifact_row_by_id(
        self, artifact_id: str, db: Session
    ) -> Optional[GeneratedArtifact]:
        return db.get(GeneratedArtifact, artifact_id)

    def mark_uploading(
        self, artifact_id: str, db: Optional[Session] = None
    ) -> Optional[GeneratedArtifactModel]:
        with get_db_context(db) as db:
            artifact = self.get_artifact_row_by_id(artifact_id, db)
            if not artifact or artifact.publication_state not in ACTIVE_PUBLICATION_STATES:
                return None
            artifact.publication_state = ARTIFACT_STATE_UPLOADING
            artifact.updated_at = now_ts()
            db.commit()
            db.refresh(artifact)
            return GeneratedArtifactModel.model_validate(artifact)

    def mark_published(
        self,
        artifact_id: str,
        *,
        size_bytes: Optional[int],
        content_type: Optional[str],
        content_sha256: Optional[str],
        etag: Optional[str],
        db: Optional[Session] = None,
    ) -> Optional[GeneratedArtifactModel]:
        timestamp = now_ts()
        with get_db_context(db) as db:
            artifact = self.get_artifact_row_by_id(artifact_id, db)
            if not artifact:
                return None
            if artifact.publication_state == ARTIFACT_STATE_PUBLISHED:
                return GeneratedArtifactModel.model_validate(artifact)
            if artifact.publication_state not in ACTIVE_PUBLICATION_STATES:
                return None
            artifact.publication_state = ARTIFACT_STATE_PUBLISHED
            artifact.failure_reason = None
            artifact.size_bytes = size_bytes
            artifact.content_type = content_type
            artifact.content_sha256 = content_sha256
            artifact.etag = etag
            artifact.published_at = timestamp
            artifact.updated_at = timestamp
            db.commit()
            db.refresh(artifact)
            return GeneratedArtifactModel.model_validate(artifact)

    def mark_failed(
        self,
        artifact_id: str,
        *,
        failure_reason: str,
        db: Optional[Session] = None,
    ) -> Optional[GeneratedArtifactModel]:
        with get_db_context(db) as db:
            artifact = self.get_artifact_row_by_id(artifact_id, db)
            if not artifact:
                return None
            if artifact.publication_state == ARTIFACT_STATE_FAILED:
                return GeneratedArtifactModel.model_validate(artifact)
            if artifact.publication_state in (
                ARTIFACT_STATE_DELETED,
                ARTIFACT_STATE_PUBLISHED,
            ):
                return None
            artifact.publication_state = ARTIFACT_STATE_FAILED
            artifact.failure_reason = failure_reason
            artifact.updated_at = now_ts()
            db.commit()
            db.refresh(artifact)
            return GeneratedArtifactModel.model_validate(artifact)

    def soft_delete_artifact(
        self, artifact_id: str, db: Optional[Session] = None
    ) -> Optional[GeneratedArtifactModel]:
        timestamp = now_ts()
        with get_db_context(db) as db:
            artifact = self.get_artifact_row_by_id(artifact_id, db)
            if not artifact:
                return None
            artifact.publication_state = ARTIFACT_STATE_DELETED
            artifact.deleted_at = timestamp
            artifact.updated_at = timestamp
            db.commit()
            db.refresh(artifact)
            return GeneratedArtifactModel.model_validate(artifact)

    def mark_stale_publications_failed(
        self,
        *,
        stale_before: int,
        limit: int,
        db: Optional[Session] = None,
    ) -> int:
        timestamp = now_ts()
        with get_db_context(db) as db:
            rows = (
                db.query(GeneratedArtifact)
                .filter(GeneratedArtifact.publication_state.in_(ACTIVE_PUBLICATION_STATES))
                .filter(GeneratedArtifact.updated_at <= stale_before)
                .limit(limit)
                .all()
            )
            for artifact in rows:
                artifact.publication_state = ARTIFACT_STATE_FAILED
                artifact.failure_reason = "stale_publication_session"
                artifact.updated_at = timestamp
            db.commit()
            return len(rows)


GeneratedArtifacts = GeneratedArtifactsTable()
