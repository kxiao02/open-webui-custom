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
from open_webui.utils.portal_sso import PortalSSOManager


class TestEnterpriseOAuth(AbstractPostgresTest):
    def _snapshot_portal_sso_config(self):
        return {
            "enabled": config.PORTAL_SSO_ENABLED.value,
            "provider_name": config.PORTAL_SSO_PROVIDER_NAME.value,
            "app_initiated_enabled": config.PORTAL_SSO_APP_INITIATED_ENABLED.value,
            "validate_url": config.PORTAL_SSO_VALIDATE_URL.value,
            "entry_url_template": config.PORTAL_SSO_ENTRY_URL_TEMPLATE.value,
            "timeout_seconds": config.PORTAL_SSO_TIMEOUT_SECONDS.value,
            "auto_signup": config.PORTAL_SSO_AUTO_SIGNUP.value,
            "synthetic_email_domain": config.PORTAL_SSO_SYNTHETIC_EMAIL_DOMAIN.value,
        }

    def _restore_portal_sso_config(self, snapshot):
        config.PORTAL_SSO_ENABLED.value = snapshot["enabled"]
        config.PORTAL_SSO_PROVIDER_NAME.value = snapshot["provider_name"]
        config.PORTAL_SSO_APP_INITIATED_ENABLED.value = snapshot[
            "app_initiated_enabled"
        ]
        config.PORTAL_SSO_VALIDATE_URL.value = snapshot["validate_url"]
        config.PORTAL_SSO_ENTRY_URL_TEMPLATE.value = snapshot["entry_url_template"]
        config.PORTAL_SSO_TIMEOUT_SECONDS.value = snapshot["timeout_seconds"]
        config.PORTAL_SSO_AUTO_SIGNUP.value = snapshot["auto_signup"]
        config.PORTAL_SSO_SYNTHETIC_EMAIL_DOMAIN.value = snapshot[
            "synthetic_email_domain"
        ]

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
                "account_name": "System Mint",
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
            assert user.name == "System Mint"
            assert user.oauth and "enterprise" in user.oauth
            assert user.oauth["enterprise"].get("sub") == "sysmintest"
            assert user.oauth["enterprise"].get("account_no") == "sysmintest"
            assert user.oauth["enterprise"].get("main_account_id") == "sysadmin"
            assert user.oauth["enterprise"].get("actual_name") == "System Mint"

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

    def test_enterprise_oauth_links_existing_portal_user_by_account_no(
        self, monkeypatch
    ):
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        userinfo = {
            "id": "sysadmin",
            "attributes": {
                "account_no": "sysmintest",
                "account_name": "System Mint",
            },
        }

        original_providers = self._setup_enterprise_provider()
        prev_signup = config.ENABLE_OAUTH_SIGNUP.value
        prev_domains = config.OAUTH_ALLOWED_DOMAINS.value
        try:
            config.ENABLE_OAUTH_SIGNUP.value = True
            config.OAUTH_ALLOWED_DOMAINS.value = ["*"]

            portal_user = Users.insert_new_user(
                id="portal-user",
                name="Portal Name",
                email="portal@example.com",
                role="user",
                oauth={
                    "portal": {
                        "sub": "GLOBAL-1",
                        "account_no": "sysmintest",
                    }
                },
                db=self.db,
            )
            assert portal_user is not None

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

            linked_user = Users.get_user_by_id("portal-user", db=self.db)
            assert linked_user is not None
            assert linked_user.oauth["enterprise"]["sub"] == "sysmintest"
            assert linked_user.oauth["portal"]["sub"] == "GLOBAL-1"
        finally:
            config.ENABLE_OAUTH_SIGNUP.value = prev_signup
            config.OAUTH_ALLOWED_DOMAINS.value = prev_domains
            self._restore_providers(original_providers)

    def test_portal_sso_callback_creates_user_and_sets_name_from_nick_name(
        self, monkeypatch
    ):
        snapshot = self._snapshot_portal_sso_config()
        try:
            config.PORTAL_SSO_ENABLED.value = True
            config.PORTAL_SSO_VALIDATE_URL.value = "https://portal.example.com/casValidate"
            config.PORTAL_SSO_AUTO_SIGNUP.value = True
            config.PORTAL_SSO_SYNTHETIC_EMAIL_DOMAIN.value = "portal.example.com"

            request = self.make_request(
                "/sso/portal/callback",
                query_string=b"ticket=t1",
            )
            manager = PortalSSOManager(request.app)

            async def _fake_validate(_ticket):
                return {
                    "portal_sub": "GLOBAL-USER-1",
                    "account_no": "sysmintest",
                    "actual_name": "张三",
                    "display_name": "sysmintest",
                    "nick_name": "张三",
                    "email": "",
                    "roles": ["common"],
                    "tenant_id": "tenant-1",
                    "company_name": "company-1",
                    "telephone": "",
                }

            monkeypatch.setattr(manager, "_validate_ticket", _fake_validate)

            redirect = self.run_async(manager.handle_callback(request, db=self.db))

            user = Users.get_user_by_oauth_sub("portal", "GLOBAL-USER-1", db=self.db)
            assert user is not None
            assert user.name == "张三"
            assert user.oauth["portal"]["account_no"] == "sysmintest"
            assert user.oauth["portal"]["nick_name"] == "张三"
            assert redirect.headers["location"].endswith("/auth")
        finally:
            self._restore_portal_sso_config(snapshot)

    def test_portal_sso_login_requires_app_initiated_enablement(self):
        snapshot = self._snapshot_portal_sso_config()
        try:
            config.PORTAL_SSO_ENABLED.value = True
            config.PORTAL_SSO_APP_INITIATED_ENABLED.value = False
            config.PORTAL_SSO_ENTRY_URL_TEMPLATE.value = (
                "https://portal.example.com/login?service={callback}"
            )

            request = self.make_request("/sso/portal/login")
            manager = PortalSSOManager(request.app)

            try:
                self.run_async(manager.handle_login(request))
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError(
                    "Expected portal app-initiated login to be disabled"
                )
        finally:
            self._restore_portal_sso_config(snapshot)

    def test_portal_sso_login_builds_redirect_when_app_initiated_enabled(self):
        snapshot = self._snapshot_portal_sso_config()
        try:
            config.PORTAL_SSO_ENABLED.value = True
            config.PORTAL_SSO_APP_INITIATED_ENABLED.value = True
            config.PORTAL_SSO_ENTRY_URL_TEMPLATE.value = (
                "https://portal.example.com/login?service={callback}"
            )

            request = self.make_request(
                "/sso/portal/login",
                query_string=b"redirect=%2Fworkspace",
            )
            manager = PortalSSOManager(request.app)

            redirect = self.run_async(manager.handle_login(request))

            assert redirect.status_code in {302, 307}
            assert (
                redirect.headers["location"]
                == "https://portal.example.com/login?service=http%3A%2F%2Ftestserver%2Fsso%2Fportal%2Fcallback%3Fredirect%3D%252Fworkspace"
            )
        finally:
            self._restore_portal_sso_config(snapshot)

    def test_portal_sso_callback_links_existing_enterprise_user_by_account_no(
        self, monkeypatch
    ):
        snapshot = self._snapshot_portal_sso_config()
        try:
            config.PORTAL_SSO_ENABLED.value = True
            config.PORTAL_SSO_VALIDATE_URL.value = "https://portal.example.com/casValidate"
            config.PORTAL_SSO_AUTO_SIGNUP.value = True

            enterprise_user = Users.insert_new_user(
                id="enterprise-user",
                name="System Mint",
                email="sysmintest@example.com",
                role="user",
                oauth={"enterprise": {"sub": "sysmintest", "account_no": "sysmintest"}},
                db=self.db,
            )
            assert enterprise_user is not None

            request = self.make_request(
                "/sso/portal/callback",
                query_string=b"ticket=t2",
            )
            manager = PortalSSOManager(request.app)

            async def _fake_validate(_ticket):
                return {
                    "portal_sub": "GLOBAL-USER-2",
                    "account_no": "sysmintest",
                    "actual_name": "张三",
                    "display_name": "sysmintest",
                    "nick_name": "张三",
                    "email": "sysmintest@example.com",
                    "roles": ["common"],
                    "tenant_id": "tenant-1",
                    "company_name": "company-1",
                    "telephone": "",
                }

            monkeypatch.setattr(manager, "_validate_ticket", _fake_validate)

            self.run_async(manager.handle_callback(request, db=self.db))

            linked_user = Users.get_user_by_id("enterprise-user", db=self.db)
            assert linked_user is not None
            assert linked_user.oauth["portal"]["sub"] == "GLOBAL-USER-2"
            assert linked_user.oauth["portal"]["account_no"] == "sysmintest"
        finally:
            self._restore_portal_sso_config(snapshot)
