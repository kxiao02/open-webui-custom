import copy
import os
import time

os.environ.setdefault("ENABLE_DB_MIGRATIONS", "False")

import open_webui.config as config
import open_webui.utils.oauth as oauth_utils
from open_webui.models.groups import GroupForm, GroupMember, Groups
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import Users
from fastapi import HTTPException
from open_webui.routers import auths, configs
from open_webui.test_support import AbstractPostgresTest
from open_webui.utils.oauth import OAuthManager


class TestEnterpriseOAuth(AbstractPostgresTest):
    def _setup_enterprise_provider(self, enterprise_cfg: dict | None = None):
        original_providers = copy.deepcopy(config.OAUTH_PROVIDERS)
        config.OAUTH_PROVIDERS.clear()
        config.OAUTH_PROVIDERS["enterprise"] = {
            "register": lambda _oauth: object(),
            "type": "enterprise",
            "enterprise": enterprise_cfg
            or {
                "client_id": "enterprise-client",
                "client_secret": "enterprise-secret",
                "authorize_url": "https://sso.example.com/oauth2.0/authorize",
                "token_url": "https://sso.example.com/oauth2.0/accessToken",
                "profile_url": "https://sso.example.com/oauth2.0/profile",
                "check_token_url": "https://sso.example.com/api/v1/loginLog/checkAccessToken",
                "logout_url": "https://sso.example.com/cxf/api/v1/ssoSession/remove",
                "redirect_uri": "https://webui.example.com/oauth/enterprise/callback",
                "authorize_redirect_param": "redirect_url",
                "token_redirect_param": "redirect.uri",
                "id_claim": "id",
                "account_no_path": "attributes.account_no",
                "email_claim": "",
                "email_domain": "example.com",
            },
        }
        return original_providers

    def _restore_providers(self, original_providers):
        config.OAUTH_PROVIDERS.clear()
        config.OAUTH_PROVIDERS.update(original_providers)

    def test_enterprise_oauth_callback_creates_user_and_session(self, monkeypatch):
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        userinfo = {
            "id": "sysadmin",
            "attributes": {
                "account_no": "sysmintest",
                "token_expire": "3600",
                "token_gtime": str(int(time.time() * 1000)),
            },
        }

        original_providers = self._setup_enterprise_provider()
        prev_signup = config.ENABLE_OAUTH_SIGNUP.value
        prev_domains = config.OAUTH_ALLOWED_DOMAINS.value
        try:
            config.ENABLE_OAUTH_SIGNUP.value = True
            config.OAUTH_ALLOWED_DOMAINS.value = ["*"]

            request = self.make_request(
                "/oauth/enterprise/login/callback",
                query_string=b"code=code",
            )
            response = self.make_response()
            manager = OAuthManager(request.app)

            async def _fake_token(_cfg, _params):
                return token

            async def _fake_profile(_cfg, _access_token):
                return userinfo

            monkeypatch.setattr(manager, "_enterprise_request_token", _fake_token)
            monkeypatch.setattr(manager, "_enterprise_fetch_profile", _fake_profile)

            self.run_async(
                manager.handle_callback(request, "enterprise", response, db=self.db)
            )

            user = Users.get_user_by_email("sysmintest@example.com", db=self.db)
            assert user is not None
            assert user.oauth and "enterprise" in user.oauth
            assert user.oauth["enterprise"].get("account_no") == "sysmintest"

            sessions = OAuthSessions.get_sessions_by_user_id(user.id, db=self.db)
            assert sessions
            assert sessions[0].token.get("access_token") == "access-token"
        finally:
            config.ENABLE_OAUTH_SIGNUP.value = prev_signup
            config.OAUTH_ALLOWED_DOMAINS.value = prev_domains
            self._restore_providers(original_providers)

    def test_enterprise_oauth_callback_assigns_default_group(self, monkeypatch):
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        userinfo = {
            "id": "group-user",
            "attributes": {"account_no": "groupuser"},
        }

        group = Groups.insert_new_group(
            "admin-1",
            form_data=GroupForm(
                name="Default",
                description="Default group",
            ),
            db=self.db,
        )
        assert group is not None

        original_providers = self._setup_enterprise_provider()
        prev_signup = config.ENABLE_OAUTH_SIGNUP.value
        prev_domains = config.OAUTH_ALLOWED_DOMAINS.value
        try:
            config.ENABLE_OAUTH_SIGNUP.value = True
            config.OAUTH_ALLOWED_DOMAINS.value = ["*"]

            request = self.make_request(
                "/oauth/enterprise/login/callback",
                query_string=b"code=code",
            )
            request.app.state.config.DEFAULT_GROUP_ID = group.id
            response = self.make_response()
            manager = OAuthManager(request.app)

            async def _fake_token(_cfg, _params):
                return token

            async def _fake_profile(_cfg, _access_token):
                return userinfo

            monkeypatch.setattr(manager, "_enterprise_request_token", _fake_token)
            monkeypatch.setattr(manager, "_enterprise_fetch_profile", _fake_profile)

            self.run_async(
                manager.handle_callback(request, "enterprise", response, db=self.db)
            )

            user = Users.get_user_by_email("groupuser@example.com", db=self.db)
            assert user is not None
            membership = (
                self.db.query(GroupMember)
                .filter_by(group_id=group.id, user_id=user.id)
                .first()
            )
            assert membership is not None
        finally:
            config.ENABLE_OAUTH_SIGNUP.value = prev_signup
            config.OAUTH_ALLOWED_DOMAINS.value = prev_domains
            self._restore_providers(original_providers)

    def test_enterprise_oauth_callback_rolls_back_user_on_default_group_failure(
        self, monkeypatch
    ):
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        userinfo = {
            "id": "rollback-user",
            "attributes": {"account_no": "rollbackuser"},
        }

        original_providers = self._setup_enterprise_provider()
        prev_signup = config.ENABLE_OAUTH_SIGNUP.value
        prev_domains = config.OAUTH_ALLOWED_DOMAINS.value
        try:
            config.ENABLE_OAUTH_SIGNUP.value = True
            config.OAUTH_ALLOWED_DOMAINS.value = ["*"]

            request = self.make_request(
                "/oauth/enterprise/login/callback",
                query_string=b"code=code",
            )
            request.app.state.config.DEFAULT_GROUP_ID = "default-group"
            response = self.make_response()
            manager = OAuthManager(request.app)

            async def _fake_token(_cfg, _params):
                return token

            async def _fake_profile(_cfg, _access_token):
                return userinfo

            def _explode(*_args, **_kwargs):
                raise RuntimeError("default group assignment failed")

            monkeypatch.setattr(manager, "_enterprise_request_token", _fake_token)
            monkeypatch.setattr(manager, "_enterprise_fetch_profile", _fake_profile)
            monkeypatch.setattr(oauth_utils, "apply_default_group_assignment", _explode)

            redirect = self.run_async(
                manager.handle_callback(request, "enterprise", response, db=self.db)
            )

            assert "error=" in redirect.headers["location"]
            assert Users.get_user_by_email("rollbackuser@example.com", db=self.db) is None
        finally:
            config.ENABLE_OAUTH_SIGNUP.value = prev_signup
            config.OAUTH_ALLOWED_DOMAINS.value = prev_domains
            self._restore_providers(original_providers)

    def test_enterprise_oauth_refresh_updates_session_token(self, monkeypatch):
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 1,
        }
        userinfo = {
            "id": "sysadmin",
            "attributes": {"account_no": "sysmintest"},
        }

        original_providers = self._setup_enterprise_provider()
        prev_signup = config.ENABLE_OAUTH_SIGNUP.value
        prev_domains = config.OAUTH_ALLOWED_DOMAINS.value
        try:
            config.ENABLE_OAUTH_SIGNUP.value = True
            config.OAUTH_ALLOWED_DOMAINS.value = ["*"]

            request = self.make_request(
                "/oauth/enterprise/login/callback",
                query_string=b"code=code",
            )
            response = self.make_response()
            manager = OAuthManager(request.app)

            async def _fake_token(_cfg, _params):
                return token

            async def _fake_profile(_cfg, _access_token):
                return userinfo

            monkeypatch.setattr(manager, "_enterprise_request_token", _fake_token)
            monkeypatch.setattr(manager, "_enterprise_fetch_profile", _fake_profile)
            self.run_async(
                manager.handle_callback(request, "enterprise", response, db=self.db)
            )

            user = Users.get_user_by_email("sysmintest@example.com", db=self.db)
            assert user is not None

            sessions = OAuthSessions.get_sessions_by_user_id(user.id, db=self.db)
            assert sessions
            session_id = sessions[0].id

            async def _refresh_token_request(_cfg, _params):
                return {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                }

            monkeypatch.setattr(
                manager, "_enterprise_request_token", _refresh_token_request
            )
            refreshed = self.run_async(
                manager.get_oauth_token(user.id, session_id, force_refresh=True)
            )
            assert refreshed["access_token"] == "new-access"

            updated = OAuthSessions.get_session_by_id(session_id, db=self.db)
            assert updated.token["access_token"] == "new-access"
        finally:
            config.ENABLE_OAUTH_SIGNUP.value = prev_signup
            config.OAUTH_ALLOWED_DOMAINS.value = prev_domains
            self._restore_providers(original_providers)

    def test_enterprise_oauth_signout_revokes_session(self, monkeypatch):
        original_providers = self._setup_enterprise_provider()
        try:
            user = Users.insert_new_user(
                id="u1",
                name="Enterprise User",
                email="sysmintest@example.com",
                role="user",
            )
            assert user is not None

            token = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": int(time.time()) + 3600,
            }
            session = OAuthSessions.create_session(
                user_id=user.id, provider="enterprise", token=token, db=self.db
            )
            assert session is not None

            request = self.make_request(
                "/api/v1/auths/signout",
                headers={"cookie": f"oauth_session_id={session.id}"},
            )
            response = self.make_response()
            manager = OAuthManager(request.app)
            request.app.state.oauth_manager = manager

            called = {"value": False}

            async def _fake_revoke(provider, access_token):
                called["value"] = provider == "enterprise" and access_token == "access-token"
                return True

            monkeypatch.setattr(manager, "enterprise_revoke_token", _fake_revoke)

            signout_response = self.run_async(
                auths.signout(request=request, response=response, db=self.db)
            )
            assert signout_response.status_code == 200
            assert called["value"] is True
            assert OAuthSessions.get_session_by_id(session.id, db=self.db) is None
        finally:
            self._restore_providers(original_providers)

    def test_runtime_enterprise_oauth_updates_are_blocked_when_deployment_managed(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            configs, "is_enterprise_oauth_deployment_managed", lambda: True
        )

        request = self.make_request("/api/v1/configs/enterprise_oauth", method="POST")
        form_data = configs.EnterpriseOAuthConfigForm(
            ENABLE_ENTERPRISE_OAUTH=True,
            CLIENT_ID="blocked-client",
        )

        try:
            self.run_async(
                configs.set_enterprise_oauth_config(
                    request=request,
                    form_data=form_data,
                    user=None,
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "deployment-managed" in exc.detail
        else:
            raise AssertionError("Expected Enterprise OAuth runtime update to be rejected")
