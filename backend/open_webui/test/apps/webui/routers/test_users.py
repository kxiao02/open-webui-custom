from open_webui.test_support import AbstractPostgresTest
from open_webui.models.users import UserSettings, UserUpdateForm, Users
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

    def test_users(self):
        admin = Users.update_user_by_id(
            "1",
            {
                "name": "user 1",
                "email": "user1@openwebui.com",
                "profile_image_url": "/user1.png",
                "role": "admin",
            },
            db=self.db,
        )
        user_two = Users.get_user_by_id("2", db=self.db)

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, "1", role="admin")
        _assert_user(data, "2")

        updated = self.run_async(
            users.update_user_by_id(
                user_id="2",
                form_data=UserUpdateForm(
                    name="user 2",
                    email="user2@openwebui.com",
                    profile_image_url="/user2.png",
                    role="admin",
                ),
                session_user=admin,
                db=self.db,
            )
        )
        _assert_user([updated], "2", role="admin")
        user_two = Users.get_user_by_id("2", db=self.db)

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, "1", role="admin")
        _assert_user(data, "2", role="admin")

        response = self.run_async(
            users.get_user_settings_by_session_user(user=user_two, db=self.db)
        )
        assert response is None

        request = self.make_request(
            self.create_url("/user/settings/update"),
            method="POST",
        )
        response = self.run_async(
            users.update_user_settings_by_session_user(
                request=request,
                form_data=UserSettings(ui={"attr1": "value1", "attr2": "value2"}),
                user=user_two,
                db=self.db,
            )
        )
        assert response.model_dump() == {"ui": {"attr1": "value1", "attr2": "value2"}}

        response = self.run_async(
            users.get_user_settings_by_session_user(user=user_two, db=self.db)
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

        details = self.run_async(users.get_user_by_id("2", user=admin, db=self.db))
        assert details.name == "user 2"
        assert details.profile_image_url == "/user2.png"
        assert details.is_active is True

        updated = self.run_async(
            users.update_user_by_id(
                user_id="2",
                form_data=UserUpdateForm(
                    name="user 2 updated",
                    email="user2-updated@openwebui.com",
                    profile_image_url="/user2-updated.png",
                    role="admin",
                ),
                session_user=admin,
                db=self.db,
            )
        )
        assert updated.name == "user 2 updated"

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 2
        data = payload["users"]
        _assert_user(data, "1", role="admin")
        _assert_user(
            data,
            "2",
            role="admin",
            name="user 2 updated",
            email="user2-updated@openwebui.com",
            profile_image_url="/user2-updated.png",
        )

        deleted = self.run_async(users.delete_user_by_id("2", user=admin, db=self.db))
        assert deleted is True

        payload = self.run_async(users.get_users(user=admin, db=self.db))
        assert payload["total"] == 1
        _assert_user(payload["users"], "1", role="admin")
