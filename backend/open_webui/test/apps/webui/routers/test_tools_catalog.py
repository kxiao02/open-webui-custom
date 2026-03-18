from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from open_webui.models.tools import ToolForm, Tools
from open_webui.routers import tools
from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.utils import tools as tool_utils


def _insert_tool(db, tool_id: str, *, meta: dict):
    return Tools.insert_new_tool(
        "admin-1",
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

    def test_non_admin_cannot_create_or_update_tools(self):
        with mock_webui_user(id="2", role="user") as user:
            with pytest.raises(HTTPException) as create_exc:
                self.run_async(
                    tools.create_new_tools(
                        request=self.request,
                        form_data=ToolForm(
                            id="user_tool",
                            name="user_tool",
                            content="pass",
                            meta={"description": "tool"},
                        ),
                        user=user,
                        db=self.db,
                    )
                )
            assert create_exc.value.status_code == 401

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
