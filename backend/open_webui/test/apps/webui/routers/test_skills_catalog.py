import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from open_webui.models.skills import SkillForm, Skills
from open_webui.models.resource_installations import ResourceInstallations
from open_webui.routers import skills
from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.utils.skill_import import seed_minimax_skills_once, sync_minimax_skills
from open_webui.utils.middleware import process_chat_payload
from open_webui.utils.tools import get_builtin_tools


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

    def test_sync_minimax_skills_missing_default_runtime_mount_returns_actionable_error(self):
        with mock_webui_user(id="admin-1", role="admin") as user:
            with patch(
                "open_webui.routers.skills.sync_minimax_skill_catalog",
                side_effect=FileNotFoundError(
                    "MiniMax skills directory not found: /app/minimax-skills/skills"
                ),
            ):
                with pytest.raises(HTTPException) as exc_info:
                    self.run_async(
                        skills.sync_minimax_skills(
                            form_data=skills.SkillSyncForm(),
                            user=user,
                            db=self.db,
                        )
                    )

        assert exc_info.value.status_code == 400
        assert "MiniMax skills directory not found" in exc_info.value.detail
        assert "open-webui-skill-seed" in exc_info.value.detail
        assert "root_path" in exc_info.value.detail


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

    def test_process_chat_payload_enables_dynamic_skill_loading_for_visible_skills(self):
        model = {
            "id": "probe-dynamic-skills",
            "info": {
                "meta": {
                    "capabilities": {
                        "file_context": False,
                        "builtin_tools": True,
                    }
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
                    id="meeting-skill",
                    name="会议纪要通知生成",
                    description="Generate meeting summaries and notifications",
                    content="# meeting skill",
                    meta={"published": False, "visibility": "hidden"},
                    access_grants=[],
                ),
                db=self.db,
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
                            "model": "probe-dynamic-skills",
                            "messages": [{"role": "user", "content": "帮我整理会议纪要"}],
                        },
                        user,
                        {"params": {"function_calling": "native"}},
                        model,
                    )
                )

        system_text = _system_text(form_data["messages"])
        assert "【技能动态加载】" in system_text
        assert "list_skills" in system_text
        assert "view_skill" in system_text
        assert captured["skill_ids"] == ["meeting-skill"]
        runtime_snapshot = form_data["metadata"]["deepagent_runtime_tools"]
        runtime_tool_names = {
            function_entry["registered_name"]
            for tool_entry in runtime_snapshot["tools"]
            for function_entry in tool_entry.get("functions", [])
        }
        assert "list_skills" in runtime_tool_names
        assert "view_skill" in runtime_tool_names
        assert runtime_snapshot["context"]["metadata"]["deepagent_runtime_skill_ids"] == [
            "meeting-skill"
        ]

    def test_process_chat_payload_auto_attaches_named_workspace_skill(self):
        model = {
            "id": "probe-dynamic-skills",
            "info": {
                "meta": {
                    "capabilities": {
                        "file_context": False,
                        "builtin_tools": True,
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
                    id="meeting-skill",
                    name="会议纪要通知生成",
                    description="Generate meeting summaries and notifications",
                    content="# meeting skill",
                    meta={"published": False, "visibility": "hidden"},
                    access_grants=[],
                ),
                db=self.db,
            )

            form_data, _, _ = self.run_async(
                process_chat_payload(
                    self.request,
                    {
                        "model": "probe-dynamic-skills",
                        "messages": [
                            {
                                "role": "user",
                                "content": "请使用工作区技能 会议纪要通知生成，按它的要求处理。",
                            }
                        ],
                    },
                    user,
                    {"params": {"function_calling": "native"}},
                    model,
                )
            )

        system_text = _system_text(form_data["messages"])
        assert '<skill name="会议纪要通知生成">' in system_text
        assert "# meeting skill" in system_text

    def test_get_builtin_tools_freezes_dynamic_skill_ids_for_skill_loaders(self):
        model = {
            "id": "probe-dynamic-skills",
            "info": {
                "meta": {
                    "capabilities": {
                        "builtin_tools": True,
                    }
                }
            },
        }

        tools = get_builtin_tools(
            self.request,
            {
                "__user__": {"id": "user-1"},
                "__skill_ids__": ["meeting-skill"],
            },
            {},
            model,
        )

        assert tools["list_skills"]["callable"].__extra_params__["__skill_ids__"] == [
            "meeting-skill"
        ]
        assert tools["view_skill"]["callable"].__extra_params__["__skill_ids__"] == [
            "meeting-skill"
        ]

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


class TestSkillImportSync(AbstractPostgresTest):
    def test_sync_minimax_skills_discovers_all_skill_directories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "fullstack-dev").mkdir()
            (tmp_path / "fullstack-dev" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: Fullstack Dev
                    description: Build full-stack apps
                    metadata:
                      category: full-stack
                    ---

                    # Fullstack Dev
                    """
                ),
                encoding="utf-8",
            )
            (tmp_path / "minimax-pdf").mkdir()
            (tmp_path / "minimax-pdf" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: MiniMax PDF
                    description: Generate PDFs
                    metadata:
                      category: document-generation
                    ---

                    # MiniMax PDF
                    """
                ),
                encoding="utf-8",
            )

            result = sync_minimax_skills(
                user_id="admin-1",
                root_path=str(tmp_path),
                db=self.db,
            )

        assert sorted(result["created"]) == ["fullstack-dev", "minimax-pdf"]
        assert result["errors"] == []

        fullstack_skill = Skills.get_skill_by_id("fullstack-dev", db=self.db)
        assert fullstack_skill is not None
        assert fullstack_skill.meta.category == "full-stack"

        document_skill = Skills.get_skill_by_id("minimax-pdf", db=self.db)
        assert document_skill is not None
        assert "中电慧语文档能力" in document_skill.content

    def test_seed_minimax_skills_once_only_creates_missing_default_document_skills(self):
        Skills.insert_new_skill(
            "admin-1",
            SkillForm(
                id="minimax-pdf",
                name="Existing PDF Skill",
                description="existing",
                content="# existing content",
                meta={"published": True, "visibility": "public"},
                access_grants=[],
            ),
            db=self.db,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "minimax-pdf").mkdir()
            (tmp_path / "minimax-pdf" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: MiniMax PDF
                    description: Generate PDFs
                    ---

                    # MiniMax PDF
                    """
                ),
                encoding="utf-8",
            )
            (tmp_path / "minimax-docx").mkdir()
            (tmp_path / "minimax-docx" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: MiniMax DOCX
                    description: Generate DOCX
                    ---

                    # MiniMax DOCX
                    """
                ),
                encoding="utf-8",
            )
            (tmp_path / "fullstack-dev").mkdir()
            (tmp_path / "fullstack-dev" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: Fullstack Dev
                    description: Build apps
                    ---

                    # Fullstack Dev
                    """
                ),
                encoding="utf-8",
            )

            result = seed_minimax_skills_once(
                root_path=str(tmp_path),
                user_id="admin-1",
                db=self.db,
            )

        assert result["created"] == ["minimax-docx"]
        assert result["updated"] == []
        assert result["skipped_existing"] == ["minimax-pdf"]
        assert result["errors"] == []

        existing_pdf = Skills.get_skill_by_id("minimax-pdf", db=self.db)
        assert existing_pdf is not None
        assert existing_pdf.content == "# existing content"

        seeded_docx = Skills.get_skill_by_id("minimax-docx", db=self.db)
        assert seeded_docx is not None

        unrelated_skill = Skills.get_skill_by_id("fullstack-dev", db=self.db)
        assert unrelated_skill is None
