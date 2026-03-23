import pytest
from fastapi import HTTPException

from open_webui.test_support import AbstractPostgresTest
from open_webui.models.users import User, UserSettings, UserUpdateForm, Users
from open_webui.routers import users


def _as_dict(entry):
    return entry.model_dump() if hasattr(entry, "model_dump") else entry


def _get_user_by_id(data, param):
    return next((item for item in data if _as_dict(item)["id"] == param), None)


def _assert_user(data, id, **kwargs):
    user = _get_user_by_id(data, id)
    assert user is not None
    payload = _as_dict(user)
    comparison_data = {
        "name": f"user {id}",
        "email": f"user{id}@openwebui.com",
        "profile_image_url": f"/user{id}.png",
        "role": "user",
        **kwargs,
    }
    for key, value in comparison_data.items():
        assert payload[key] == value


class TestUsers(AbstractPostgresTest):
    BASE_PATH = "/api/v1/users"

    def setup_method(self):
        super().setup_method()
        Users.insert_new_user(
            id="1",
            name="user 1",
            email="user1@openwebui.com",
            profile_image_url="/user1.png",
            role="user",
        )
        Users.insert_new_user(
            id="2",
            name="user 2",
            email="user2@openwebui.com",
            profile_image_url="/user2.png",
            role="user",
        )
        self.db.query(User).filter_by(id="1").update(
            {"created_at": 1, "updated_at": 1, "last_active_at": 1}
        )
        self.db.query(User).filter_by(id="2").update(
            {"created_at": 2, "updated_at": 2, "last_active_at": 2}
        )
        self.db.commit()

    def test_users(self):
        async def passthrough_reconcile(_request, _user, settings_payload):
            return settings_payload or {}, False

        users._reconcile_user_model_settings = passthrough_reconcile

        primary_user = Users.get_first_user(db=self.db)
        assert primary_user is not None

        admin_id = primary_user.id
        target_user_id = "2" if admin_id == "1" else "1"
        target_name = f"user {target_user_id}"
        target_email = f"user{target_user_id}@openwebui.com"
        target_updated_name = f"user {target_user_id} updated"
        target_updated_email = f"user{target_user_id}-updated@openwebui.com"

        admin = Users.update_user_by_id(
            admin_id,
            {
                "name": f"user {admin_id}",
                "email": f"user{admin_id}@openwebui.com",
                "profile_image_url": f"/user{admin_id}.png",
                "role": "admin",
            },
            db=self.db,
        )
        target_user = Users.get_user_by_id(target_user_id, db=self.db)

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, admin_id, role="admin")
        _assert_user(data, target_user_id)

        updated = self.run_async(
            users.update_user_by_id(
                user_id=target_user_id,
                form_data=UserUpdateForm(
                    name=target_name,
                    email=target_email,
                    profile_image_url="/user.png",
                    role="admin",
                ),
                session_user=admin,
                db=self.db,
            )
        )
        _assert_user(
            [updated],
            target_user_id,
            role="admin",
            profile_image_url="/user.png",
        )
        target_user = Users.get_user_by_id(target_user_id, db=self.db)

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, admin_id, role="admin")
        _assert_user(
            data,
            target_user_id,
            role="admin",
            profile_image_url="/user.png",
        )

        response = self.run_async(
            users.get_user_settings_by_session_user(
                request=self.make_request(self.create_url("/user/settings")),
                user=target_user,
                db=self.db,
            )
        )
        assert response.model_dump() == {"ui": {}}

        request = self.make_request(
            self.create_url("/user/settings/update"),
            method="POST",
        )
        response = self.run_async(
            users.update_user_settings_by_session_user(
                request=request,
                form_data=UserSettings(ui={"attr1": "value1", "attr2": "value2"}),
                user=target_user,
                db=self.db,
            )
        )
        assert response.model_dump() == {"ui": {"attr1": "value1", "attr2": "value2"}}

        response = self.run_async(
            users.get_user_settings_by_session_user(
                request=self.make_request(self.create_url("/user/settings")),
                user=target_user,
                db=self.db,
            )
        )
        assert response.model_dump() == {"ui": {"attr1": "value1", "attr2": "value2"}}

        response = self.run_async(
            users.get_user_info_by_session_user(user=admin, db=self.db)
        )
        assert response is None

        response = self.run_async(
            users.update_user_info_by_session_user(
                form_data={"attr1": "value1", "attr2": "value2"},
                user=admin,
                db=self.db,
            )
        )
        assert response == {"attr1": "value1", "attr2": "value2"}

        response = self.run_async(
            users.get_user_info_by_session_user(user=admin, db=self.db)
        )
        assert response == {"attr1": "value1", "attr2": "value2"}

        details = self.run_async(
            users.get_user_by_id(target_user_id, user=admin, db=self.db)
        )
        assert details.name == target_name
        assert details.profile_image_url == "/user.png"
        assert details.is_active is False

        updated = self.run_async(
            users.update_user_by_id(
                user_id=target_user_id,
                form_data=UserUpdateForm(
                    name=target_updated_name,
                    email=target_updated_email,
                    profile_image_url="/user.png",
                    role="admin",
                ),
                session_user=admin,
                db=self.db,
            )
        )
        assert updated.name == target_updated_name

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, admin_id, role="admin")
        _assert_user(
            data,
            target_user_id,
            role="admin",
            name=target_updated_name,
            email=target_updated_email,
            profile_image_url="/user.png",
        )

        deleted = self.run_async(
            users.delete_user_by_id(target_user_id, user=admin, db=self.db)
        )
        assert deleted is True

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 1
        _assert_user(payload["users"], admin_id, role="admin")

    def test_only_primary_admin_is_protected_from_delete(self):
        first_admin = Users.update_user_by_id(
            "2",
            {
                "name": "user 2",
                "email": "user2@openwebui.com",
                "profile_image_url": "/user2.png",
                "role": "admin",
            },
            db=self.db,
        )

        deleted = self.run_async(
            users.delete_user_by_id("1", user=first_admin, db=self.db)
        )
        assert deleted is True
        assert Users.get_user_by_id("1", db=self.db) is None

        with pytest.raises(HTTPException) as exc_info:
            self.run_async(users.delete_user_by_id("2", user=first_admin, db=self.db))
        assert exc_info.value.status_code == 403
