from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from open_webui.models.tools import ToolForm, Tools
from open_webui.routers import tools
from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.utils import tools as tool_utils


VALID_TOOL_CONTENT = """
class Tools:
    def ping(self) -> str:
        return "pong"
""".strip()


def _insert_tool(db, tool_id: str, *, meta: dict, owner_id: str = "admin-1"):
    return Tools.insert_new_tool(
        owner_id,
        ToolForm(
            id=tool_id,
            name=tool_id,
            content="pass",
            meta=meta,
            access_grants=[],
        ),
        specs=[
            {
                "name": f"{tool_id}_call",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
        ],
        db=db,
    )


class TestToolCatalogRouters(AbstractPostgresTest):
    BASE_PATH = "/api/v1/tools"

    def setup_method(self):
        super().setup_method()
        self.request = self.make_request(self.create_url("/"))
        self.request.app.state.TOOLS = {}
        self.request.app.state.config.TOOL_SERVER_CONNECTIONS = []

    def test_non_admin_can_create_own_tool_but_cannot_update_others(self):
        with mock_webui_user(id="2", role="user") as user:
            created = self.run_async(
                tools.create_new_tools(
                    request=self.request,
                    form_data=ToolForm(
                        id="user_tool",
                        name="user_tool",
                        content=VALID_TOOL_CONTENT,
                        meta={"description": "tool"},
                    ),
                    user=user,
                    db=self.db,
                )
            )
            assert created is not None
            assert created.user_id == "2"
            assert created.id == "user_tool"

            _insert_tool(
                self.db,
                "existing_tool",
                meta={"description": "tool", "published": True, "visibility": "public"},
            )

            with pytest.raises(HTTPException) as update_exc:
                self.run_async(
                    tools.update_tools_by_id(
                        request=self.request,
                        id="existing_tool",
                        form_data=ToolForm(
                            id="existing_tool",
                            name="existing_tool",
                            content="pass",
                            meta={"description": "updated", "published": True, "visibility": "public"},
                        ),
                        user=user,
                        db=self.db,
                    )
                )
            assert update_exc.value.status_code == 401

    def test_get_tool_by_id_hides_unpublished_tools_from_non_admin(self):
        _insert_tool(
            self.db,
            "draft_tool",
            meta={"description": "draft", "published": False, "visibility": "public"},
        )
        _insert_tool(
            self.db,
            "public_tool",
            meta={"description": "public", "published": True, "visibility": "public"},
        )

        with mock_webui_user(id="2", role="user") as user:
            with pytest.raises(HTTPException) as exc_info:
                self.run_async(tools.get_tools_by_id("draft_tool", user=user, db=self.db))
            assert exc_info.value.status_code == 401

            visible = self.run_async(tools.get_tools_by_id("public_tool", user=user, db=self.db))
            assert visible.id == "public_tool"
            assert visible.write_access is False

    def test_owner_can_view_unpublished_hidden_custom_tool(self):
        _insert_tool(
            self.db,
            "owner_draft_tool",
            meta={"description": "owner", "published": False, "visibility": "hidden"},
            owner_id="user-1",
        )

        with mock_webui_user(id="user-1", role="user") as owner:
            visible = self.run_async(
                tools.get_tools_by_id("owner_draft_tool", user=owner, db=self.db)
            )
            assert visible.id == "owner_draft_tool"
            assert visible.write_access is True

        with mock_webui_user(id="user-2", role="user") as other:
            with pytest.raises(HTTPException) as exc_info:
                self.run_async(
                    tools.get_tools_by_id("owner_draft_tool", user=other, db=self.db)
                )
            assert exc_info.value.status_code == 401

    def test_non_admin_cannot_update_default_tool_even_if_owner(self):
        _insert_tool(
            self.db,
            "default_tool",
            meta={
                "description": "default",
                "published": True,
                "visibility": "public",
                "is_default": True,
            },
            owner_id="user-1",
        )

        with mock_webui_user(id="user-1", role="user") as user:
            with pytest.raises(HTTPException) as exc_info:
                self.run_async(
                    tools.update_tools_by_id(
                        request=self.request,
                        id="default_tool",
                        form_data=ToolForm(
                            id="default_tool",
                            name="default_tool",
                            content="pass",
                            meta={
                                "description": "updated",
                                "published": True,
                                "visibility": "public",
                                "is_default": True,
                            },
                        ),
                        user=user,
                        db=self.db,
                    )
                )
            assert exc_info.value.status_code == 401

    def test_owner_write_access_for_custom_tool(self):
        _insert_tool(
            self.db,
            "owner_tool",
            meta={"description": "owner", "published": True, "visibility": "public"},
            owner_id="user-1",
        )

        with mock_webui_user(id="user-1", role="user") as owner:
            visible = self.run_async(tools.get_tools_by_id("owner_tool", user=owner, db=self.db))
            assert visible.write_access is True

        with mock_webui_user(id="user-2", role="user") as other:
            visible = self.run_async(tools.get_tools_by_id("owner_tool", user=other, db=self.db))
            assert visible.write_access is False

    def test_runtime_get_tools_filters_unpublished_tools(self):
        _insert_tool(
            self.db,
            "public_tool",
            meta={"description": "public", "published": True, "visibility": "public"},
        )
        _insert_tool(
            self.db,
            "draft_tool",
            meta={"description": "draft", "published": False, "visibility": "public"},
        )

        async def public_tool_call():
            """public tool"""

            return "ok"

        async def draft_tool_call():
            """draft tool"""

            return "nope"

        self.request.app.state.TOOLS = {
            "public_tool": SimpleNamespace(public_tool_call=public_tool_call),
            "draft_tool": SimpleNamespace(draft_tool_call=draft_tool_call),
        }

        with mock_webui_user(id="2", role="user") as user:
            resolved = self.run_async(
                tool_utils.get_tools(
                    self.request,
                    ["public_tool", "draft_tool"],
                    user,
                    {"__user__": {"id": user.id, "role": user.role}},
                )
            )

        assert sorted(resolved.keys()) == ["public_tool_call"]

    def test_get_tool_list_includes_server_tools(self, monkeypatch):
        server_id = "server-1"
        self.request.app.state.config.TOOL_SERVER_CONNECTIONS = [
            {
                "config": {
                    "access_grants": [
                        {
                            "principal_type": "user",
                            "principal_id": "*",
                            "permission": "read",
                        }
                    ]
                }
            }
        ]

        async def _fake_get_tool_servers(_request):
            return [
                {
                    "id": server_id,
                    "idx": 0,
                    "openapi": {
                        "info": {
                            "title": "Server Tool",
                            "description": "Server tool description",
                        }
                    },
                    "specs": [],
                }
            ]

        monkeypatch.setattr(tools, "get_tool_servers", _fake_get_tool_servers)

        with mock_webui_user(id="2", role="user") as user:
            result = self.run_async(
                tools.get_tool_list(request=self.request, user=user, db=self.db)
            )

        server_tool = next((tool for tool in result if tool.id == f"server:{server_id}"), None)
        assert server_tool is not None
        assert server_tool.meta.origin == "external"
        assert server_tool.meta.source_of_truth == "openapi-tool-server"
        assert server_tool.meta.execution_boundary == "external"
        assert server_tool.write_access is False
        assert server_tool.catalog_kind == "external"

    def test_get_tool_list_normalizes_custom_tool_metadata(self):
        _insert_tool(
            self.db,
            "public_tool",
            meta={"description": "public", "published": True, "visibility": "public"},
        )

        with mock_webui_user(id="2", role="user") as user:
            result = self.run_async(
                tools.get_tool_list(request=self.request, user=user, db=self.db)
            )

        custom_tool = next((tool for tool in result if tool.id == "public_tool"), None)
        assert custom_tool is not None
        assert custom_tool.meta.origin == "host"
        assert custom_tool.meta.source_of_truth == "open-webui-tool"
        assert custom_tool.meta.execution_boundary == "open-webui"
        assert custom_tool.catalog_kind == "custom"

    def test_get_tool_list_includes_core_catalog_tools(self, monkeypatch):
        async def _fake_get_core_tool_entries(_request):
            return [
                {
                    "id": "internet_search",
                    "user_id": "system:agent-core",
                    "name": "网络搜索",
                    "meta": {
                        "description": "Run internet search using Bocha API.",
                        "origin": "core",
                        "catalog_kind": "core",
                        "source_of_truth": "agent-core",
                        "execution_boundary": "agent",
                        "mutability": "locked",
                        "available": True,
                        "availability_state": "registered",
                    },
                    "access_grants": [],
                    "updated_at": 1,
                    "created_at": 1,
                    "write_access": False,
                    "installed": False,
                    "installable": False,
                    "catalog_kind": "core",
                }
            ]

        monkeypatch.setattr(tools, "_get_core_tool_entries", _fake_get_core_tool_entries)

        with mock_webui_user(id="2", role="user") as user:
            result = self.run_async(
                tools.get_tool_list(request=self.request, user=user, db=self.db)
            )

        core_tool = next((tool for tool in result if tool.id == "internet_search"), None)
        assert core_tool is not None
        assert core_tool.meta.origin == "core"
        assert core_tool.meta.source_of_truth == "agent-core"
        assert core_tool.meta.execution_boundary == "agent"
        assert core_tool.write_access is False
        assert core_tool.installed is False
        assert core_tool.installable is False
        assert core_tool.catalog_kind == "core"

    def test_get_builtin_tool_list_returns_catalog(self):
        with mock_webui_user(id="2", role="user") as user:
            result = self.run_async(
                tools.get_builtin_tool_list(request=self.request, user=user)
            )

        tool_ids = {tool["id"] if isinstance(tool, dict) else tool.id for tool in result}
        assert {
            "time",
            "memory",
            "chats",
            "notes",
            "knowledge",
            "channels",
            "web_search",
            "image_generation",
            "code_interpreter",
        }.issubset(tool_ids)
        host_tool = next(
            tool for tool in result if (tool["id"] if isinstance(tool, dict) else tool.id) == "web_search"
        )
        host_meta = host_tool["meta"] if isinstance(host_tool, dict) else host_tool.meta
        host_origin = host_meta["origin"] if isinstance(host_meta, dict) else host_meta.origin
        host_boundary = (
            host_meta["execution_boundary"]
            if isinstance(host_meta, dict)
            else host_meta.execution_boundary
        )
        assert host_origin == "host"
        assert host_boundary == "open-webui"

    def test_image_generation_catalog_entry_is_disabled_without_real_backend_config(self):
        self.request.app.state.config.ENABLE_IMAGE_GENERATION = True
        self.request.app.state.config.IMAGE_GENERATION_ENGINE = "openai"
        self.request.app.state.config.IMAGES_OPENAI_API_BASE_URL = ""
        self.request.app.state.config.IMAGES_OPENAI_API_KEY = ""
        self.request.app.state.config.ENABLE_IMAGE_EDIT = False

        with mock_webui_user(id="2", role="user") as user:
            result = self.run_async(
                tools.get_builtin_tool_list(request=self.request, user=user)
            )

        image_tool = next(
            tool
            for tool in result
            if (tool["id"] if isinstance(tool, dict) else tool.id) == "image_generation"
        )
        image_meta = image_tool["meta"] if isinstance(image_tool, dict) else image_tool.meta

        assert tool_utils.is_image_generation_tool_available(self.request.app.state.config) is False
        assert image_meta["available"] is False
        assert image_meta["availability_state"] == "disabled"

    def test_install_uninstall_server_tool(self, monkeypatch):
        server_id = "server-1"
        tool_id = f"server:{server_id}"
        self.request.app.state.config.TOOL_SERVER_CONNECTIONS = [
            {
                "config": {
                    "access_grants": [
                        {
                            "principal_type": "user",
                            "principal_id": "*",
                            "permission": "read",
                        }
                    ]
                }
            }
        ]

        async def _fake_get_tool_servers(_request):
            return [
                {
                    "id": server_id,
                    "idx": 0,
                    "openapi": {
                        "info": {
                            "title": "Server Tool",
                            "description": "Server tool description",
                        }
                    },
                    "specs": [],
                }
            ]

        monkeypatch.setattr(tools, "get_tool_servers", _fake_get_tool_servers)

        with mock_webui_user(id="2", role="user") as user:
            installed = self.run_async(
                tools.install_tools_by_id(
                    request=self.request, id=tool_id, user=user, db=self.db
                )
            )
            assert installed["id"] == tool_id
            assert installed["installed"] is True

            removed = self.run_async(
                tools.uninstall_tools_by_id(
                    request=self.request, id=tool_id, user=user, db=self.db
                )
            )
            assert removed["id"] == tool_id
            assert removed["installed"] is False
