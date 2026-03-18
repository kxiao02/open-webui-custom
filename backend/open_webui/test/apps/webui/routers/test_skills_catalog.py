import pytest
from fastapi import HTTPException

from open_webui.models.skills import SkillForm, Skills
from open_webui.routers import skills
from open_webui.test_support import AbstractPostgresTest, mock_webui_user


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


class TestSkillCatalogRouters(AbstractPostgresTest):
    BASE_PATH = "/api/v1/skills"

    def setup_method(self):
        super().setup_method()
        self.request = self.make_request(self.create_url("/"))

    def test_non_admin_cannot_create_or_toggle_skills(self):
        with mock_webui_user(id="2", role="user") as user:
            with pytest.raises(HTTPException) as create_exc:
                self.run_async(
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
            assert create_exc.value.status_code == 401

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
