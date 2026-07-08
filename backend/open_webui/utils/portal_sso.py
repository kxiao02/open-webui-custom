import hashlib
import logging
import re
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

import aiohttp
from fastapi import HTTPException, status
from starlette.responses import RedirectResponse

from open_webui.config import (
    JWT_EXPIRES_IN,
    PORTAL_SSO_AUTO_SIGNUP,
    PORTAL_SSO_APP_INITIATED_ENABLED,
    PORTAL_SSO_ENTRY_URL_TEMPLATE,
    PORTAL_SSO_ENABLED,
    PORTAL_SSO_SYNTHETIC_EMAIL_DOMAIN,
    PORTAL_SSO_TIMEOUT_SECONDS,
    PORTAL_SSO_VALIDATE_URL,
)
from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    WEBUI_AUTH_COOKIE_SAME_SITE,
    WEBUI_AUTH_COOKIE_SECURE,
)
from open_webui.internal.db import get_db_context
from open_webui.models.auths import Auths
from open_webui.models.users import Users
from open_webui.utils.auth import create_token, get_password_hash
from open_webui.utils.groups import apply_default_group_assignment
from open_webui.utils.misc import parse_duration

log = logging.getLogger(__name__)


def _local_name(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


class PortalSSOManager:
    def __init__(self, app):
        self.app = app

    def _webui_base_url(self, request) -> str:
        return str(request.app.state.config.WEBUI_URL or request.base_url).rstrip("/")

    def _sanitize_redirect_path(self, redirect_path: Optional[str]) -> Optional[str]:
        if not redirect_path:
            return None

        parsed = urllib.parse.urlparse(redirect_path)
        if parsed.scheme or parsed.netloc:
            return None

        if redirect_path.startswith("//"):
            return None

        if not redirect_path.startswith("/"):
            redirect_path = f"/{redirect_path}"

        return redirect_path

    def _build_auth_redirect_url(
        self,
        request,
        *,
        redirect_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> str:
        params: list[tuple[str, str]] = []
        safe_redirect = self._sanitize_redirect_path(redirect_path)
        if safe_redirect:
            params.append(("redirect", safe_redirect))
        if error:
            params.append(("error", error))

        query = urllib.parse.urlencode(params)
        base = f"{self._webui_base_url(request)}/auth"
        return f"{base}?{query}" if query else base

    def _build_post_login_redirect_url(
        self, request, *, redirect_path: Optional[str] = None
    ) -> str:
        safe_redirect = self._sanitize_redirect_path(redirect_path) or "/"
        return f"{self._webui_base_url(request)}{safe_redirect}"

    def _build_callback_url(self, request, redirect_path: Optional[str]) -> str:
        params = []
        safe_redirect = self._sanitize_redirect_path(redirect_path)
        if safe_redirect:
            params.append(("redirect", safe_redirect))

        query = urllib.parse.urlencode(params)
        base = f"{self._webui_base_url(request)}/sso/portal/callback"
        return f"{base}?{query}" if query else base

    def _build_entry_url(self, request, redirect_path: Optional[str]) -> str:
        template = str(PORTAL_SSO_ENTRY_URL_TEMPLATE.value or "").strip()
        if not template:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Portal SSO entry URL template is not configured",
            )

        callback_url = self._build_callback_url(request, redirect_path)
        replacements = {
            "callback": urllib.parse.quote(callback_url, safe=""),
            "callback_raw": callback_url,
            "redirect": urllib.parse.quote(redirect_path or "", safe=""),
            "redirect_raw": redirect_path or "",
        }

        try:
            return template.format(**replacements)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Portal SSO entry URL template is missing placeholder {exc}",
            ) from exc

    def _build_synthetic_email(self, portal_sub: str) -> str:
        digest = hashlib.sha256(portal_sub.encode("utf-8")).hexdigest()[:16]
        domain = re.sub(
            r"[^a-zA-Z0-9.-]",
            "",
            str(PORTAL_SSO_SYNTHETIC_EMAIL_DOMAIN.value or "portal.local"),
        ).lower()
        if not domain:
            domain = "portal.local"
        return f"portal-{digest}@{domain}"

    def _extract_portal_profile(self, xml_text: str, status_code: int) -> dict:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Portal SSO returned malformed XML",
            ) from exc

        success_node = None
        failure_node = None

        for node in root.iter():
            node_name = _local_name(node.tag)
            if node_name == "authenticationSuccess":
                success_node = node
                break
            if node_name == "authenticationFailure":
                failure_node = node

        if success_node is None:
            message = "Portal SSO validation failed"
            if failure_node is not None:
                code = failure_node.attrib.get("code", "").strip()
                reason = (failure_node.text or "").strip()
                parts = [part for part in [code, reason] if part]
                if parts:
                    message = ": ".join([message, " - ".join(parts)])
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED
                if status_code in {401, 403}
                else status.HTTP_400_BAD_REQUEST,
                detail=message,
            )

        portal_sub = ""
        attributes = {}
        for child in success_node:
            child_name = _local_name(child.tag)
            if child_name == "user":
                portal_sub = (child.text or "").strip()
            elif child_name == "attributes":
                for attribute in child:
                    attributes[_local_name(attribute.tag)] = (attribute.text or "").strip()

        if not portal_sub:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Portal SSO validation response is missing cas:user",
            )

        account_no = attributes.get("display_name", "").strip() or portal_sub
        actual_name = attributes.get("nickName", "").strip() or account_no or portal_sub
        email = attributes.get("email", "").strip().lower()
        roles = [
            value.strip()
            for value in re.split(r"[;,]", attributes.get("roles", ""))
            if value.strip()
        ]

        return {
            "portal_sub": portal_sub,
            "account_no": account_no,
            "actual_name": actual_name,
            "display_name": attributes.get("display_name", "").strip(),
            "nick_name": attributes.get("nickName", "").strip(),
            "email": email,
            "roles": roles,
            "tenant_id": attributes.get("tenantId", "").strip(),
            "company_name": attributes.get("companyName", "").strip(),
            "telephone": attributes.get("telephone", "").strip(),
        }

    async def _validate_ticket(self, ticket: str) -> dict:
        validate_url = str(PORTAL_SSO_VALIDATE_URL.value or "").strip()
        if not validate_url:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Portal SSO validate URL is not configured",
            )

        separator = "&" if "?" in validate_url else "?"
        request_url = f"{validate_url}{separator}ticket={urllib.parse.quote(ticket, safe='')}"
        timeout = aiohttp.ClientTimeout(total=int(PORTAL_SSO_TIMEOUT_SECONDS.value or 10))

        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(request_url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as response:
                xml_text = await response.text()
                return self._extract_portal_profile(xml_text, response.status)

    def _resolve_display_name(self, profile: dict) -> str:
        return (
            profile.get("actual_name")
            or profile.get("account_no")
            or profile.get("portal_sub")
        )

    def _find_user_for_profile(self, profile: dict, db) -> Optional[object]:
        portal_sub = profile["portal_sub"]
        account_no = profile.get("account_no") or ""
        email = (profile.get("email") or "").lower()

        user = Users.get_user_by_oauth_sub("portal", portal_sub, db=db)
        if user:
            return user

        if account_no:
            user = Users.get_user_by_oauth_sub("enterprise", account_no, db=db)
            if user:
                return user

            user = Users.get_user_by_oauth_provider_field(
                "portal", "account_no", account_no, db=db
            )
            if user:
                return user

        if email:
            user = Users.get_user_by_email(email, db=db)
            if user:
                return user

        return None

    def _build_portal_payload(self, profile: dict, *, synthetic_email: bool) -> dict:
        return {
            "sub": profile["portal_sub"],
            "account_no": profile.get("account_no") or "",
            "display_name": profile.get("display_name") or "",
            "nick_name": profile.get("nick_name") or "",
            "tenant_id": profile.get("tenant_id") or "",
            "company_name": profile.get("company_name") or "",
            "telephone": profile.get("telephone") or "",
            "roles": profile.get("roles") or [],
            "email": profile.get("email") or "",
            "email_is_synthetic": synthetic_email,
        }

    def _upsert_user(self, request, profile: dict, db=None):
        actual_name = self._resolve_display_name(profile)
        real_email = (profile.get("email") or "").lower()
        email_is_synthetic = not bool(real_email)
        email = real_email or self._build_synthetic_email(profile["portal_sub"])
        payload = self._build_portal_payload(profile, synthetic_email=email_is_synthetic)

        with get_db_context(db) as db_session:
            try:
                user = self._find_user_for_profile(profile, db_session)
                if user:
                    Users.update_user_oauth_by_id(
                        user.id,
                        "portal",
                        profile["portal_sub"],
                        payload=payload,
                        db=db_session,
                        commit=False,
                    )

                    if actual_name and actual_name != user.name:
                        Users.update_user_by_id(
                            user.id, {"name": actual_name}, db=db_session, commit=False
                        )
                        user.name = actual_name

                    if real_email and real_email != user.email.lower():
                        existing_user = Users.get_user_by_email(real_email, db=db_session)
                        if not existing_user or existing_user.id == user.id:
                            Auths.update_email_by_id(
                                user.id,
                                real_email,
                                db=db_session,
                                commit=False,
                            )
                            user.email = real_email
                else:
                    if not PORTAL_SSO_AUTO_SIGNUP.value:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Portal SSO signup is disabled",
                        )

                    existing_user = Users.get_user_by_email(email, db=db_session)
                    if existing_user:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Portal SSO email is already in use",
                        )

                    role = (
                        "admin"
                        if not Users.has_users(db=db_session)
                        else request.app.state.config.DEFAULT_USER_ROLE
                    )
                    user = Auths.insert_new_auth(
                        email=email,
                        password=get_password_hash(str(uuid.uuid4())),
                        name=actual_name,
                        profile_image_url="/user.png",
                        role=role,
                        oauth={"portal": payload},
                        db=db_session,
                        commit=False,
                    )
                    apply_default_group_assignment(
                        request.app.state.config.DEFAULT_GROUP_ID,
                        user.id,
                        db=db_session,
                        commit=False,
                    )

                db_session.commit()
                return user
            except HTTPException:
                db_session.rollback()
                raise
            except Exception as exc:
                db_session.rollback()
                log.exception("Portal SSO user upsert failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Portal SSO user upsert failed",
                ) from exc

    def _build_session_redirect(self, request, user, redirect_path: Optional[str]):
        expires_in = getattr(
            request.app.state.config, "JWT_EXPIRES_IN", JWT_EXPIRES_IN.value
        )
        jwt_token = create_token(
            data={"id": user.id},
            expires_delta=parse_duration(expires_in),
        )
        redirect_url = self._build_post_login_redirect_url(
            request, redirect_path=redirect_path
        )
        response = RedirectResponse(url=redirect_url)
        response.set_cookie(
            key="token",
            value=jwt_token,
            httponly=False,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
        )
        return response

    async def handle_login(self, request):
        if not PORTAL_SSO_ENABLED.value or not PORTAL_SSO_APP_INITIATED_ENABLED.value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        redirect_path = self._sanitize_redirect_path(
            request.query_params.get("redirect")
        )

        try:
            entry_url = self._build_entry_url(request, redirect_path)
        except HTTPException as exc:
            return RedirectResponse(
                url=self._build_auth_redirect_url(
                    request, redirect_path=redirect_path, error=exc.detail
                )
            )

        return RedirectResponse(url=entry_url)

    async def handle_callback(self, request, db=None):
        if not PORTAL_SSO_ENABLED.value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        redirect_path = self._sanitize_redirect_path(
            request.query_params.get("redirect")
        )

        try:
            ticket = (request.query_params.get("ticket") or "").strip()
            if not ticket:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Portal SSO ticket is missing",
                )

            profile = await self._validate_ticket(ticket)
            user = self._upsert_user(request, profile, db=db)
            return self._build_session_redirect(request, user, redirect_path)
        except HTTPException as exc:
            return RedirectResponse(
                url=self._build_auth_redirect_url(
                    request, redirect_path=redirect_path, error=exc.detail
                )
            )
