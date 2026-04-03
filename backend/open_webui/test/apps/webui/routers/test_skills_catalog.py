from unittest.mock import patch

import pytest
from fastapi import HTTPException

from open_webui.models.skills import SkillForm, Skills
from open_webui.models.resource_installations import ResourceInstallations
from open_webui.routers import skills
from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.utils.middleware import process_chat_payload


def _insert_skill(db, skill_id: str, *, meta: dict, is_active: bool = True):
    return Skills.insert_new_skill(
        "admin-1",
        SkillForm(
            id=skill_id,
            name=skill_id,
            description=skill_id,
            content=f"# {skill_id}",
            meta=meta,
            is_active=is_active,
            access_grants=[],
        ),
        db=db,
    )


def _system_text(messages: list[dict]) -> str:
    return "\n---\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )


class TestSkillCatalogRouters(AbstractPostgresTest):
    BASE_PATH = "/api/v1/skills"

    def setup_method(self):
        super().setup_method()
        self.request = self.make_request(self.create_url("/"))

    def test_non_admin_can_create_and_toggle_owned_skills(self):
        with mock_webui_user(id="2", role="user") as user:
            created = self.run_async(
                skills.create_new_skill(
                    request=self.request,
                    form_data=SkillForm(
                        id="user-skill",
                        name="user-skill",
                        description="skill",
                        content="# skill",
                        meta={"published": True, "visibility": "public"},
                    ),
                    user=user,
                    db=self.db,
                )
            )
            assert created.user_id == user.id
            assert created.meta.visibility == "restricted"
            assert created.is_active is True

            toggled = self.run_async(
                skills.toggle_skill_by_id(
                    id="user-skill",
                    user=user,
                    db=self.db,
                )
            )
            assert toggled.is_active is False

            _insert_skill(
                self.db,
                "existing-skill",
                meta={"published": True, "visibility": "public"},
            )

            with pytest.raises(HTTPException) as toggle_exc:
                self.run_async(
                    skills.toggle_skill_by_id(
                        id="existing-skill",
                        user=user,
                        db=self.db,
                    )
                )
            assert toggle_exc.value.status_code == 401

    def test_get_skill_list_filters_draft_hidden_and_inactive_skills(self):
        _insert_skill(
            self.db,
            "public-skill",
            meta={"published": True, "visibility": "public"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "draft-skill",
            meta={"published": False, "visibility": "public"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "hidden-skill",
            meta={"published": True, "visibility": "hidden"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "inactive-skill",
            meta={"published": True, "visibility": "public"},
            is_active=False,
        )

        with mock_webui_user(id="2", role="user") as user:
            response = self.run_async(
                skills.get_skill_list(user=user, db=self.db, query=None, view_option=None, page=1)
            )
            assert response.total == 1
            assert [item.id for item in response.items] == ["public-skill"]
            assert response.items[0].write_access is False

            visible = self.run_async(skills.get_skill_by_id("public-skill", user=user, db=self.db))
            assert visible.id == "public-skill"

            with pytest.raises(HTTPException) as exc_info:
                self.run_async(skills.get_skill_by_id("draft-skill", user=user, db=self.db))
            assert exc_info.value.status_code == 401


class TestSkillAgentInjection(AbstractPostgresTest):
    def setup_method(self):
        super().setup_method()
        self.request = self.make_request("/")
        self.request.app.state.config.TASK_MODEL = ""
        self.request.app.state.config.TASK_MODEL_EXTERNAL = False

    def test_process_chat_payload_injects_selected_and_installed_skills(self):
        model = {
            "id": "probe-selected",
            "info": {
                "meta": {
                    "capabilities": {
                        "file_context": False,
                        "builtin_tools": False,
                    }
                }
            },
        }
        self.request.state.direct = True
        self.request.state.model = model

        with mock_webui_user(id="user-1", role="user") as user:
            Skills.insert_new_skill(
                user.id,
                SkillForm(
                    id="selected-skill",
                    name="Selected Skill",
                    description="selected",
                    content="# selected content",
                    meta={"published": False, "visibility": "hidden"},
                    access_grants=[],
                ),
                db=self.db,
            )
            Skills.insert_new_skill(
                user.id,
                SkillForm(
                    id="installed-skill",
                    name="Installed Skill",
                    description="installed",
                    content="# installed content",
                    meta={"published": False, "visibility": "hidden"},
                    access_grants=[],
                ),
                db=self.db,
            )
            ResourceInstallations.install_resource(
                user.id, "skill", "installed-skill", db=self.db
            )

            form_data, _, _ = self.run_async(
                process_chat_payload(
                    self.request,
                    {
                        "model": "probe-selected",
                        "messages": [{"role": "user", "content": "hello"}],
                        "skill_ids": ["selected-skill"],
                    },
                    user,
                    {"params": {}},
                    model,
                )
            )

        system_text = _system_text(form_data["messages"])
        assert '<skill name="Selected Skill">' in system_text
        assert "# selected content" in system_text
        assert '<skill name="Installed Skill">' in system_text
        assert "# installed content" in system_text

    def test_process_chat_payload_passes_model_attached_skills_to_builtin_tools(self):
        model = {
            "id": "probe-native",
            "info": {
                "meta": {
                    "skillIds": ["model-skill"],
                    "capabilities": {
                        "file_context": False,
                        "builtin_tools": True,
                    },
                }
            },
        }
        self.request.state.direct = True
        self.request.state.model = model

        captured = {}

        with mock_webui_user(id="user-1", role="user") as user:
            Skills.insert_new_skill(
                user.id,
                SkillForm(
                    id="selected-skill",
                    name="Selected Skill",
                    description="selected",
                    content="# selected content",
                    meta={"published": False, "visibility": "hidden"},
                    access_grants=[],
                ),
                db=self.db,
            )
            _insert_skill(
                self.db,
                "model-skill",
                meta={"published": True, "visibility": "public"},
            )

            def fake_get_builtin_tools(_request, extra_params, _features, _model):
                captured["skill_ids"] = list(extra_params.get("__skill_ids__", []))
                return {}

            with patch(
                "open_webui.utils.middleware.get_builtin_tools",
                side_effect=fake_get_builtin_tools,
            ):
                form_data, _, _ = self.run_async(
                    process_chat_payload(
                        self.request,
                        {
                            "model": "probe-native",
                            "messages": [{"role": "user", "content": "hello"}],
                            "skill_ids": ["selected-skill"],
                        },
                        user,
                        {"params": {"function_calling": "native"}},
                        model,
                    )
                )

        system_text = _system_text(form_data["messages"])
        assert '<skill name="Selected Skill">' in system_text
        assert "# selected content" in system_text
        assert "<available_skills>" in system_text
        assert "<name>model-skill</name>" in system_text
        assert captured["skill_ids"] == ["model-skill"]

    def test_process_chat_payload_injects_workspace_draft_capabilities(self):
        model = {
            "id": "probe-drafts",
            "info": {
                "meta": {
                    "capabilities": {
                        "file_context": False,
                        "builtin_tools": False,
                    }
                }
            },
        }
        self.request.state.direct = True
        self.request.state.model = model

        with mock_webui_user(id="user-1", role="user") as user:
            form_data, _, _ = self.run_async(
                process_chat_payload(
                    self.request,
                    {
                        "model": "probe-drafts",
                        "messages": [{"role": "user", "content": "你能创建新工具或技能吗？"}],
                    },
                    user,
                    {"params": {}},
                    model,
                )
            )

        system_text = _system_text(form_data["messages"])
        assert "workspace_tool_draft.enabled=true" in system_text
        assert "workspace_skill_draft.enabled=true" in system_text
        assert "不要把“不能自动静默创建”误说成“完全不能创建”" in system_text
        assert "```python 代码块" in system_text
        assert "```markdown 代码块" in system_text
