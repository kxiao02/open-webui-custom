"""Scoped artifact publication context for agent-originated artifact operations.

Open WebUI mints a signed, time-limited, purpose-bound token after verifying
the caller's user/chat/message scope. The bridge forwards it into LangGraph
state as trusted host metadata. The agent uses it plus internal service
authentication for artifact reservation/finalization calls.

The token is NOT a general Open WebUI JWT and is NOT accepted by general APIs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

_ARTIFACT_SCOPE_PURPOSE = "artifact_publication"
_ARTIFACT_SCOPE_AUDIENCE = "generated-artifact-api"
_DEFAULT_TTL_SECONDS = 1800  # 30 minutes
_MAX_TTL_SECONDS = 3600  # 1 hour


def _signing_key() -> bytes:
    """Return the signing key for artifact publication scope tokens.

    Uses a dedicated env var if available, otherwise derives from WEBUI_SECRET_KEY
    with a purpose-separated salt to avoid cross-purpose token reuse.
    """
    dedicated = os.environ.get("ARTIFACT_PUBLICATION_SCOPE_KEY", "").strip()
    if dedicated:
        return dedicated.encode("utf-8")
    from open_webui.env import WEBUI_SECRET_KEY
    # Purpose-separated derivation: HMAC(secret, purpose) as key
    return hmac.new(
        WEBUI_SECRET_KEY.encode("utf-8"),
        _ARTIFACT_SCOPE_PURPOSE.encode("utf-8"),
        hashlib.sha256,
    ).digest()


def mint_artifact_publication_scope(
    *,
    user_id: str,
    chat_id: Optional[str] = None,
    owner_message_id: Optional[str] = None,
    run_id: Optional[str] = None,
    allowed_operations: tuple[str, ...] = ("reserve",),
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a signed, time-limited, purpose-bound artifact publication scope token.

    Returns a compact signed string that encodes the scope. The token is valid
    only for the declared operations and expires after ttl_seconds.

    Default allowed_operations is ("reserve",) only. Finalize and fail
    operations use the per-artifact publication_token returned by reservation,
    not the scope token. This keeps the scope token narrow: it authorizes
    reservation (which creates the artifact identity and publication_token)
    but does not authorize post-reservation operations.
    """
    if not user_id:
        raise ValueError("user_id is required for artifact publication scope")
    ttl = min(max(ttl_seconds, 60), _MAX_TTL_SECONDS)
    now = int(time.time())
    payload = {
        "p": _ARTIFACT_SCOPE_PURPOSE,
        "aud": _ARTIFACT_SCOPE_AUDIENCE,
        "jti": str(uuid.uuid4()),
        "uid": user_id,
        "cid": chat_id,
        "mid": owner_message_id,
        "rid": run_id,
        "ops": sorted(allowed_operations),
        "iat": now,
        "exp": now + ttl,
    }
    # Compact JSON with sorted keys for deterministic signing
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_artifact_publication_scope(
    token: str,
    *,
    required_operation: str,
    expected_user_id: Optional[str] = None,
    expected_chat_id: Optional[str] = None,
) -> dict[str, Any]:
    """Verify a scoped artifact publication context token.

    Returns the decoded scope payload on success. Raises ValueError on any
    verification failure with a descriptive message.
    """
    if not token or "." not in token:
        raise ValueError("Invalid artifact publication scope token format")

    body_str, sig = token.rsplit(".", 1)
    expected_sig = hmac.new(_signing_key(), body_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Artifact publication scope token signature mismatch")

    try:
        payload = json.loads(body_str)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid artifact publication scope token payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Artifact publication scope token payload is not a dict")

    # Purpose and audience checks
    if payload.get("p") != _ARTIFACT_SCOPE_PURPOSE:
        raise ValueError("Token purpose mismatch: expected artifact_publication")
    if payload.get("aud") != _ARTIFACT_SCOPE_AUDIENCE:
        raise ValueError("Token audience mismatch: expected generated-artifact-api")

    # Expiry check
    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < now:
        raise ValueError("Artifact publication scope token has expired")

    # Operation check
    ops = payload.get("ops")
    if not isinstance(ops, list) or required_operation not in ops:
        raise ValueError(
            f"Token does not grant '{required_operation}' operation; "
            f"allowed: {ops}"
        )

    # Scope binding checks (if caller provides expectations)
    token_uid = payload.get("uid")
    if expected_user_id is not None and token_uid != expected_user_id:
        raise ValueError("Token user_id does not match expected user")

    token_cid = payload.get("cid")
    if expected_chat_id is not None and token_cid != expected_chat_id:
        raise ValueError("Token chat_id does not match expected chat")

    return payload


def extract_scope_from_token(token: str) -> dict[str, Any]:
    """Extract scope fields from a verified token without re-verifying.

    Call only after verify_artifact_publication_scope has succeeded.
    """
    body_str = token.rsplit(".", 1)[0]
    return json.loads(body_str)
