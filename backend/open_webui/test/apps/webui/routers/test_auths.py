import os

os.environ.setdefault("ENABLE_DB_MIGRATIONS", "False")

import pytest
from fastapi import HTTPException

from open_webui.test_support import AbstractPostgresTest
from open_webui.models.auths import (
    AddUserForm,
    Auths,
    SigninForm,
    SignupForm,
    UpdatePasswordForm,
)
from open_webui.models.users import UpdateProfileForm, Users
from open_webui.models.groups import Groups, GroupMember, GroupForm
from open_webui.routers import auths
from open_webui.utils.auth import (
    create_admin_user,
    get_current_user,
    get_http_authorization_cred,
    get_password_hash,
    verify_password,
)


class TestAuths(AbstractPostgresTest):
    BASE_PATH = "/api/v1/auths"

    def test_signup_signin_and_session_user(self):
        signup_request = self.make_request(self.create_url("/signup"), method="POST")
        signup_response = self.make_response()

        signed_up = self.run_async(
            auths.signup(
                request=signup_request,
                response=signup_response,
                form_data=SignupForm(
                    name="Admin User",
                    email="admin@example.com",
                    password="StrongPass123!",
                ),
                db=self.db,
            )
        )
        assert signed_up["email"] == "admin@example.com"
        assert signed_up["role"] == "admin"
        assert signup_request.app.state.config.ENABLE_SIGNUP is False

        signin_request = self.make_request(self.create_url("/signin"), method="POST")
        signin_response = self.make_response()
        signed_in = self.run_async(
            auths.signin(
                request=signin_request,
                response=signin_response,
                form_data=SigninForm(
                    email="admin@example.com",
                    password="StrongPass123!",
                ),
                db=self.db,
            )
        )
        assert signed_in["id"] == signed_up["id"]
        assert signed_in["token"]

        token = signed_in["token"]
        session_request = self.make_request(
            self.create_url("/"),
            headers={"Authorization": f"Bearer {token}"},
        )
        auth_token = get_http_authorization_cred(f"Bearer {token}")
        session_user = self.run_async(
            get_current_user(
                session_request,
                self.make_response(),
                self.make_background_tasks(),
                auth_token=auth_token,
            )
        )
        session = self.run_async(
            auths.get_session_user(
                request=session_request,
                response=self.make_response(),
                user=session_user,
                db=self.db,
            )
        )
        assert session["email"] == "admin@example.com"
        assert session["role"] == "admin"
        assert session["permissions"]["features"]["api_keys"] is True

    def test_profile_password_and_api_key_lifecycle(self):
        user = Auths.insert_new_auth(
            email="user@example.com",
            password=get_password_hash("StrongPass123!"),
            name="Example User",
            role="user",
            db=self.db,
        )

        updated = self.run_async(
            auths.update_profile(
                form_data=UpdateProfileForm(
                    name="Updated User",
                    profile_image_url="/user.png",
                    bio="bio",
                    gender="other",
                ),
                session_user=user,
                db=self.db,
            )
        )
        assert updated.name == "Updated User"
        assert updated.profile_image_url == "/user.png"

        changed = self.run_async(
            auths.update_password(
                form_data=UpdatePasswordForm(
                    password="StrongPass123!",
                    new_password="NewStrongPass123!",
                ),
                session_user=Users.get_user_by_id(user.id, db=self.db),
                db=self.db,
            )
        )
        assert changed is True
        authenticated = Auths.authenticate_user(
            "user@example.com",
            lambda pw: verify_password("NewStrongPass123!", pw),
            db=self.db,
        )
        assert authenticated is not None

        request = self.make_request(self.create_url("/api_key"), method="POST")
        generated = self.run_async(
            auths.generate_api_key(request=request, user=authenticated, db=self.db)
        )
        assert generated["api_key"].startswith("sk-")

        fetched = self.run_async(auths.get_api_key(user=authenticated, db=self.db))
        assert fetched == generated

        deleted = self.run_async(auths.delete_api_key(user=authenticated, db=self.db))
        assert deleted is True

        with pytest.raises(HTTPException) as exc_info:
            self.run_async(auths.get_api_key(user=authenticated, db=self.db))
        assert exc_info.value.status_code == 404

    def test_add_user_and_get_admin_details(self):
        admin = Auths.insert_new_auth(
            email="root@example.com",
            password=get_password_hash("StrongPass123!"),
            name="Root User",
            role="admin",
            db=self.db,
        )

        request = self.make_request(self.create_url("/add"), method="POST")
        added = self.run_async(
            auths.add_user(
                request=request,
                form_data=AddUserForm(
                    name="Added User",
                    email="added@example.com",
                    password="StrongPass123!",
                    role="user",
                ),
                user=admin,
                db=self.db,
            )
        )
        assert added["email"] == "added@example.com"
        assert added["role"] == "user"
        assert added["token"]

        details = self.run_async(
            auths.get_admin_details(
                request=self.make_request(self.create_url("/admin/details")),
                user=admin,
                db=self.db,
            )
        )
        assert details == {"name": "Root User", "email": "root@example.com"}

    def test_signup_assigns_default_group(self):
        group = Groups.insert_new_group(
            "admin-1",
            form_data=GroupForm(
                name="Default",
                description="Default group",
            ),
            db=self.db,
        )
        assert group is not None

        signup_request = self.make_request(self.create_url("/signup"), method="POST")
        signup_request.app.state.config.DEFAULT_GROUP_ID = group.id
        signup_response = self.make_response()

        signed_up = self.run_async(
            auths.signup(
                request=signup_request,
                response=signup_response,
                form_data=SignupForm(
                    name="Group User",
                    email="group-user@example.com",
                    password="StrongPass123!",
                ),
                db=self.db,
            )
        )

        membership = (
            self.db.query(GroupMember)
            .filter_by(group_id=group.id, user_id=signed_up["id"])
            .first()
        )
        assert membership is not None

    def test_create_admin_user_repairs_existing_user_without_auth(self):
        Users.insert_new_user(
            id="u1",
            name="Legacy User",
            email="admin@example.com",
            role="user",
            db=self.db,
        )

        repaired = create_admin_user(
            "admin@example.com",
            "StrongPass123!",
            "Admin User",
        )

        assert repaired is not None
        assert repaired.email == "admin@example.com"
        assert repaired.role == "admin"

        authenticated = Auths.authenticate_user(
            "admin@example.com",
            lambda pw: verify_password("StrongPass123!", pw),
            db=self.db,
        )
        assert authenticated is not None
