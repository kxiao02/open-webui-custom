from open_webui.models.access_grants import AccessGrants
from open_webui.models.skills import SkillForm, Skills
from open_webui.models.tools import ToolForm, Tools
from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.utils.catalog import filter_visible_skills, filter_visible_tools


def _insert_tool(db, tool_id: str, *, meta: dict, access_grants: list[dict] | None = None):
    return Tools.insert_new_tool(
        "admin-1",
        ToolForm(
            id=tool_id,
            name=tool_id,
            content="pass",
            meta=meta,
            access_grants=access_grants or [],
        ),
        specs=[
            {
                "name": f"{tool_id}_call",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
        ],
        db=db,
    )


def _insert_skill(
    db,
    skill_id: str,
    *,
    meta: dict,
    is_active: bool = True,
    access_grants: list[dict] | None = None,
):
    return Skills.insert_new_skill(
        "admin-1",
        SkillForm(
            id=skill_id,
            name=skill_id,
            description=skill_id,
            content=f"# {skill_id}",
            meta=meta,
            is_active=is_active,
            access_grants=access_grants or [],
        ),
        db=db,
    )


class TestCatalogHelpers(AbstractPostgresTest):
    def test_filter_visible_tools_respects_publish_and_visibility(self):
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
        _insert_tool(
            self.db,
            "hidden_tool",
            meta={"description": "hidden", "published": True, "visibility": "hidden"},
        )
        _insert_tool(
            self.db,
            "restricted_tool",
            meta={
                "description": "restricted",
                "published": True,
                "visibility": "restricted",
            },
        )
        AccessGrants.set_access_grants(
            "tool",
            "restricted_tool",
            [{"principal_type": "user", "principal_id": "2", "permission": "read"}],
            db=self.db,
        )

        with mock_webui_user(id="2", role="user") as user:
            visible = filter_visible_tools(Tools.get_tools(defer_content=True, db=self.db), user, db=self.db)

        assert {tool.id for tool in visible} == {"restricted_tool", "public_tool"}

    def test_filter_visible_skills_requires_published_and_active(self):
        _insert_skill(
            self.db,
            "public_skill",
            meta={"published": True, "visibility": "public"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "draft_skill",
            meta={"published": False, "visibility": "public"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "hidden_skill",
            meta={"published": True, "visibility": "hidden"},
            is_active=True,
        )
        _insert_skill(
            self.db,
            "inactive_skill",
            meta={"published": True, "visibility": "public"},
            is_active=False,
        )
        _insert_skill(
            self.db,
            "restricted_skill",
            meta={"published": True, "visibility": "restricted"},
            is_active=True,
        )
        AccessGrants.set_access_grants(
            "skill",
            "restricted_skill",
            [{"principal_type": "user", "principal_id": "2", "permission": "read"}],
            db=self.db,
        )

        with mock_webui_user(id="2", role="user") as user:
            visible = filter_visible_skills(Skills.get_skills(db=self.db), user, db=self.db)

        assert [skill.id for skill in visible] == ["restricted_skill", "public_skill"]
