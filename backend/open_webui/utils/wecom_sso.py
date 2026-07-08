import hashlib
import logging
import re
import secrets
import time
import urllib.parse
import uuid
from typing import Optional

import aiohttp
from fastapi import HTTPException, status
from starlette.responses import RedirectResponse

from open_webui.config import (
    JWT_EXPIRES_IN,
    WECOM_SSO_ACCOUNT_NO_FIELD,
    WECOM_SSO_AGENT_ID,
    WECOM_SSO_AUTO_SIGNUP,
    WECOM_SSO_CALLBACK_URL,
    WECOM_SSO_CORP_ID,
    WECOM_SSO_CORP_SECRET,
    WECOM_SSO_ENABLED,
    WECOM_SSO_FETCH_USER_DETAIL,
    WECOM_SSO_PUBLIC_URL,
    WECOM_SSO_SCOPE,
    WECOM_SSO_SYNTHETIC_EMAIL_DOMAIN,
    WECOM_SSO_TIMEOUT_SECONDS,
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


class WeComSSOManager:
    STATE_COOKIE_NAME = "wecom_sso_state"
    AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
    GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    GET_USERINFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"
    GET_USER_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, app):
        self.app = app
        self._access_token: Optional[str] = None
        self._access_token_expires_at = 0

    def _public_base_url(self, request) -> str:
        configured_url = str(WECOM_SSO_PUBLIC_URL.value or "").strip()
        if configured_url:
            return configured_url.rstrip("/")

        request_base_url = str(getattr(request, "base_url", "") or "").strip()
        if request_base_url:
            return request_base_url.rstrip("/")

        return str(request.app.state.config.WEBUI_URL or "").rstrip("/")

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
        base = f"{self._public_base_url(request)}/auth"
        return f"{base}?{query}" if query else base

    def _build_post_login_redirect_url(
        self, request, *, redirect_path: Optional[str] = None
    ) -> str:
        safe_redirect = self._sanitize_redirect_path(redirect_path) or "/"
        return f"{self._public_base_url(request)}{safe_redirect}"

    def _build_callback_url(self, request, redirect_path: Optional[str]) -> str:
        callback_url = str(WECOM_SSO_CALLBACK_URL.value or "").strip()
        if not callback_url:
            callback_url = f"{self._public_base_url(request)}/sso/wecom/callback"

        params: list[tuple[str, str]] = []
        safe_redirect = self._sanitize_redirect_path(redirect_path)
        if safe_redirect and safe_redirect != "/":
            params.append(("redirect", safe_redirect))

        if not params:
            return callback_url

        separator = "&" if "?" in callback_url else "?"
        return f"{callback_url}{separator}{urllib.parse.urlencode(params)}"

    def _validate_config(self):
        if not WECOM_SSO_ENABLED.value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        missing = [
            name
            for name, value in [
                ("WECOM_SSO_CORP_ID", WECOM_SSO_CORP_ID.value),
                ("WECOM_SSO_AGENT_ID", WECOM_SSO_AGENT_ID.value),
                ("WECOM_SSO_CORP_SECRET", WECOM_SSO_CORP_SECRET.value),
            ]
            if not str(value or "").strip()
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"WeCom SSO is missing config: {', '.join(missing)}",
            )

    async def _request_json(self, url: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=int(WECOM_SSO_TIMEOUT_SECONDS.value or 10))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as response:
                payload = await response.json(content_type=None)

        errcode = payload.get("errcode", 0)
        if errcode not in (0, "0", None):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"WeCom SSO API failed: {payload.get('errmsg') or errcode}",
            )

        return payload

    async def _get_access_token(self) -> str:
        now = int(time.time())
        if self._access_token and self._access_token_expires_at > now + 60:
            return self._access_token

        query = urllib.parse.urlencode(
            {
                "corpid": str(WECOM_SSO_CORP_ID.value or "").strip(),
                "corpsecret": str(WECOM_SSO_CORP_SECRET.value or "").strip(),
            }
        )
        payload = await self._request_json(f"{self.GET_TOKEN_URL}?{query}")
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="WeCom SSO did not return access_token",
            )

        expires_in = int(payload.get("expires_in") or 7200)
        self._access_token = access_token
        self._access_token_expires_at = now + expires_in
        return access_token

    async def _get_wecom_userinfo(self, code: str) -> dict:
        access_token = await self._get_access_token()
        query = urllib.parse.urlencode({"access_token": access_token, "code": code})
        return await self._request_json(f"{self.GET_USERINFO_URL}?{query}")

    async def _get_wecom_user_detail(self, userid: str) -> dict:
        access_token = await self._get_access_token()
        query = urllib.parse.urlencode({"access_token": access_token, "userid": userid})
        return await self._request_json(f"{self.GET_USER_DETAIL_URL}?{query}")

    def _build_synthetic_email(self, userid: str) -> str:
        digest = hashlib.sha256(userid.encode("utf-8")).hexdigest()[:16]
        domain = re.sub(
            r"[^a-zA-Z0-9.-]",
            "",
            str(WECOM_SSO_SYNTHETIC_EMAIL_DOMAIN.value or "wecom.local"),
        ).lower()
        if not domain:
            domain = "wecom.local"
        return f"wecom-{digest}@{domain}"

    def _profile_value(self, profile: dict, field_name: str) -> str:
        field_name = (field_name or "").strip()
        if not field_name:
            return ""
        value = profile.get(field_name)
        if value is None:
            return ""
        return str(value).strip()

    async def _resolve_profile(self, code: str) -> dict:
        userinfo = await self._get_wecom_userinfo(code)
        userid = str(userinfo.get("UserId") or userinfo.get("userid") or "").strip()
        if not userid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="WeCom SSO did not return UserId",
            )

        detail = {}
        if WECOM_SSO_FETCH_USER_DETAIL.value:
            try:
                detail = await self._get_wecom_user_detail(userid)
            except HTTPException as exc:
                log.warning("WeCom user detail lookup failed: %s", exc.detail)

        merged = {**userinfo, **detail, "userid": userid}
        account_no = self._profile_value(
            merged, str(WECOM_SSO_ACCOUNT_NO_FIELD.value or "userid")
        )
        email = self._profile_value(merged, "email").lower()
        name = self._profile_value(merged, "name") or account_no or userid

        return {
            "userid": userid,
            "account_no": account_no or userid,
            "name": name,
            "email": email,
            "mobile": self._profile_value(merged, "mobile"),
            "alias": self._profile_value(merged, "alias"),
            "avatar": self._profile_value(merged, "avatar"),
            "department": merged.get("department") or [],
            "raw": {
                key: value
                for key, value in merged.items()
                if key not in {"access_token", "corpsecret"}
            },
        }

    def _find_user_for_profile(self, profile: dict, db) -> Optional[object]:
        userid = profile["userid"]
        account_no = profile.get("account_no") or ""
        email = (profile.get("email") or "").lower()

        user = Users.get_user_by_oauth_sub("wecom", userid, db=db)
        if user:
            return user

        if account_no:
            user = Users.get_user_by_oauth_provider_field(
                "enterprise", "account_no", account_no, db=db
            )
            if user:
                return user

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

    def _build_oauth_payload(self, profile: dict, *, synthetic_email: bool) -> dict:
        return {
            "sub": profile["userid"],
            "userid": profile["userid"],
            "account_no": profile.get("account_no") or "",
            "name": profile.get("name") or "",
            "email": profile.get("email") or "",
            "mobile": profile.get("mobile") or "",
            "alias": profile.get("alias") or "",
            "avatar": profile.get("avatar") or "",
            "department": profile.get("department") or [],
            "email_is_synthetic": synthetic_email,
        }

    def _upsert_user(self, request, profile: dict, db=None):
        real_email = (profile.get("email") or "").lower()
        email_is_synthetic = not bool(real_email)
        email = real_email or self._build_synthetic_email(profile["userid"])
        payload = self._build_oauth_payload(
            profile, synthetic_email=email_is_synthetic
        )
        name = profile.get("name") or profile.get("account_no") or profile["userid"]

        with get_db_context(db) as db_session:
            try:
                user = self._find_user_for_profile(profile, db_session)
                if user:
                    Users.update_user_oauth_by_id(
                        user.id,
                        "wecom",
                        profile["userid"],
                        payload=payload,
                        db=db_session,
                        commit=False,
                    )

                    if name and name != user.name:
                        Users.update_user_by_id(
                            user.id, {"name": name}, db=db_session, commit=False
                        )
                        user.name = name

                    if real_email and real_email != user.email.lower():
                        existing_user = Users.get_user_by_email(
                            real_email, db=db_session
                        )
                        if not existing_user or existing_user.id == user.id:
                            Auths.update_email_by_id(
                                user.id,
                                real_email,
                                db=db_session,
                                commit=False,
                            )
                            user.email = real_email
                else:
                    if not WECOM_SSO_AUTO_SIGNUP.value:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="WeCom SSO signup is disabled",
                        )

                    existing_user = Users.get_user_by_email(email, db=db_session)
                    if existing_user:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="WeCom SSO email is already in use",
                        )

                    role = (
                        "admin"
                        if not Users.has_users(db=db_session)
                        else request.app.state.config.DEFAULT_USER_ROLE
                    )
                    user = Auths.insert_new_auth(
                        email=email,
                        password=get_password_hash(str(uuid.uuid4())),
                        name=name,
                        profile_image_url=profile.get("avatar") or "/user.png",
                        role=role,
                        oauth={"wecom": payload},
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
                log.exception("WeCom SSO user upsert failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="WeCom SSO user upsert failed",
                ) from exc

    def _build_session_redirect(self, request, user, redirect_path: Optional[str]):
        expires_in = getattr(
            request.app.state.config, "JWT_EXPIRES_IN", JWT_EXPIRES_IN.value
        )
        jwt_token = create_token(
            data={"id": user.id},
            expires_delta=parse_duration(expires_in),
        )
        response = RedirectResponse(
            url=self._build_post_login_redirect_url(
                request, redirect_path=redirect_path
            )
        )
        response.set_cookie(
            key="token",
            value=jwt_token,
            httponly=False,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
        )
        response.delete_cookie(
            key=self.STATE_COOKIE_NAME,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
        )
        return response

    async def handle_login(self, request):
        self._validate_config()

        redirect_path = self._sanitize_redirect_path(
            request.query_params.get("redirect")
        )
        state = secrets.token_urlsafe(24)
        callback_url = self._build_callback_url(request, redirect_path)
        query = urllib.parse.urlencode(
            {
                "appid": str(WECOM_SSO_CORP_ID.value or "").strip(),
                "redirect_uri": callback_url,
                "response_type": "code",
                "scope": str(WECOM_SSO_SCOPE.value or "snsapi_base").strip()
                or "snsapi_base",
                "state": state,
                "agentid": str(WECOM_SSO_AGENT_ID.value or "").strip(),
            }
        )

        response = RedirectResponse(url=f"{self.AUTHORIZE_URL}?{query}#wechat_redirect")
        response.set_cookie(
            key=self.STATE_COOKIE_NAME,
            value=state,
            httponly=True,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            max_age=600,
        )
        return response

    async def handle_callback(self, request, db=None):
        if not WECOM_SSO_ENABLED.value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        redirect_path = self._sanitize_redirect_path(
            request.query_params.get("redirect")
        )

        try:
            expected_state = request.cookies.get(self.STATE_COOKIE_NAME)
            actual_state = (request.query_params.get("state") or "").strip()
            if not expected_state or not actual_state or expected_state != actual_state:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="WeCom SSO state is invalid",
                )

            code = (request.query_params.get("code") or "").strip()
            if not code:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="WeCom SSO code is missing",
                )

            profile = await self._resolve_profile(code)
            user = self._upsert_user(request, profile, db=db)
            return self._build_session_redirect(request, user, redirect_path)
        except HTTPException as exc:
            response = RedirectResponse(
                url=self._build_auth_redirect_url(
                    request, redirect_path=redirect_path, error=exc.detail
                )
            )
            response.delete_cookie(
                key=self.STATE_COOKIE_NAME,
                samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
                secure=WEBUI_AUTH_COOKIE_SECURE,
            )
            return response
