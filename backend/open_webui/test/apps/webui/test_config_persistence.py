import copy
import os

os.environ.setdefault("ENABLE_DB_MIGRATIONS", "False")

import open_webui.config as config
from fastapi import HTTPException

from open_webui.routers import configs
from open_webui.test_support import AbstractPostgresTest


def _set_nested(config_data: dict, path: str, value):
    parts = path.split(".")
    cur = config_data
    for key in parts[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


class TestConfigPersistence(AbstractPostgresTest):
    def setup_method(self):
        super().setup_method()
        config.reset_config()
        config.save_config(copy.deepcopy(config.DEFAULT_CONFIG))

    def test_oauth_config_skips_db_when_persistent_disabled(self):
        prev_oauth_persist = config.ENABLE_OAUTH_PERSISTENT_CONFIG
        try:
            config.ENABLE_OAUTH_PERSISTENT_CONFIG = False
            data = copy.deepcopy(config.DEFAULT_CONFIG)
            _set_nested(data, "oauth.oidc.client_id", "db-client-id")
            config.save_config(data)

            oauth_cfg = config.PersistentConfig(
                "OAUTH_CLIENT_ID",
                "oauth.oidc.client_id",
                "env-client-id",
            )
            assert oauth_cfg.value == "env-client-id"
        finally:
            config.ENABLE_OAUTH_PERSISTENT_CONFIG = prev_oauth_persist

    def test_non_oauth_config_persists_across_reload(self):
        data = copy.deepcopy(config.DEFAULT_CONFIG)
        _set_nested(data, "oauth.enterprise_oauth.client_id", "db-client-id")
        config.save_config(data)

        enterprise_cfg = config.PersistentConfig(
            "ENTERPRISE_OAUTH_CLIENT_ID",
            "oauth.enterprise_oauth.client_id",
            "env-client-id",
        )
        assert enterprise_cfg.value == "env-client-id"

    def test_import_rejects_enterprise_oauth_when_deployment_managed(self, monkeypatch):
        monkeypatch.setattr(
            configs, "is_enterprise_oauth_deployment_managed", lambda: True
        )

        form_data = configs.ImportConfigForm(
            config={"enterprise_oauth": {"enabled": True}}
        )

        try:
            self.run_async(configs.import_config(form_data=form_data, user=None))
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "deployment-managed" in exc.detail
        else:
            raise AssertionError("Expected Enterprise OAuth import guard to reject")

    def test_load_oauth_providers_fails_fast_for_incomplete_deployment_config(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "is_enterprise_oauth_deployment_managed", lambda: True)

        original_enabled = config.ENTERPRISE_OAUTH_ENABLED.value
        original_required = [
            (cfg.env_present, cfg.env_value, cfg.value)
            for _, cfg in config.ENTERPRISE_OAUTH_REQUIRED_FIELDS
        ]

        try:
            config.ENTERPRISE_OAUTH_ENABLED.value = True
            for index, (_, cfg) in enumerate(config.ENTERPRISE_OAUTH_REQUIRED_FIELDS):
                cfg.env_present = True
                cfg.env_value = ""
                cfg.value = ""

            config.ENTERPRISE_OAUTH_REQUIRED_FIELDS[0][1].env_value = "client-id"
            config.ENTERPRISE_OAUTH_REQUIRED_FIELDS[0][1].value = "client-id"

            try:
                config.load_oauth_providers()
            except RuntimeError as exc:
                assert "ENTERPRISE_OAUTH_CLIENT_SECRET" in str(exc)
            else:
                raise AssertionError("Expected incomplete Enterprise OAuth config to fail fast")
        finally:
            config.ENTERPRISE_OAUTH_ENABLED.value = original_enabled
            for (_, cfg), (env_present, env_value, value) in zip(
                config.ENTERPRISE_OAUTH_REQUIRED_FIELDS, original_required
            ):
                cfg.env_present = env_present
                cfg.env_value = env_value
                cfg.value = value
