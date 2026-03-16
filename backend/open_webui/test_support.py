import asyncio
import copy
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import Response
from starlette.background import BackgroundTasks
from starlette.requests import Request

import open_webui.test  # noqa: F401
from open_webui.config import DEFAULT_USER_PERMISSIONS
from open_webui.internal.db import Base, SessionLocal, engine
from open_webui.models.users import Users
from open_webui.routers import auths, models, prompts, users
from open_webui.utils.auth import create_token
from open_webui.utils.rate_limit import RateLimiter


app = FastAPI()
app.state.redis = None
TEST_USER_PERMISSIONS = copy.deepcopy(DEFAULT_USER_PERMISSIONS)
TEST_USER_PERMISSIONS["features"]["api_keys"] = True

BASE_CONFIG = {
    "USER_PERMISSIONS": TEST_USER_PERMISSIONS,
    "SHOW_ADMIN_DETAILS": True,
    "ADMIN_EMAIL": "",
    "DEFAULT_GROUP_ID": "",
    "JWT_EXPIRES_IN": "0",
    "ENABLE_SIGNUP": True,
    "ENABLE_LOGIN_FORM": True,
    "DEFAULT_USER_ROLE": "user",
    "WEBHOOK_URL": "",
    "ENABLE_API_KEYS": True,
    "ENABLE_USER_STATUS": True,
}

app.state.config = SimpleNamespace(
    **copy.deepcopy(BASE_CONFIG),
)
app.state.WEBUI_NAME = "Open WebUI"

app.include_router(auths.router, prefix="/api/v1/auths")
app.include_router(models.router, prefix="/api/v1/models")
app.include_router(prompts.router, prefix="/api/v1/prompts")
app.include_router(users.router, prefix="/api/v1/users")

Base.metadata.create_all(bind=engine)


def reset_app_config():
    app.state.config = SimpleNamespace(
        **copy.deepcopy(BASE_CONFIG),
    )


def reset_runtime_state():
    reset_app_config()
    RateLimiter._memory_store.clear()


def reset_database():
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name != 'alembic_version'
            """
        )
        for (table_name,) in cursor.fetchall():
            cursor.execute(f'DELETE FROM "{table_name}"')
        connection.commit()
    finally:
        connection.close()


def ensure_user(
    *,
    id: str,
    role: str = "user",
    name: str | None = None,
    email: str | None = None,
    profile_image_url: str | None = None,
):
    name = name or f"user {id}"
    email = email or f"user{id}@openwebui.com"
    profile_image_url = profile_image_url or f"/user{id}.png"

    user = Users.get_user_by_id(id)
    if user is None:
        user = Users.insert_new_user(
            id=id,
            name=name,
            email=email,
            profile_image_url=profile_image_url,
            role=role,
        )
    else:
        user = Users.update_user_by_id(
            id,
            {
                "name": name,
                "email": email,
                "profile_image_url": profile_image_url,
                "role": role,
            },
        )

    return user


@contextmanager
def mock_webui_user(
    *,
    id: str = "1",
    role: str = "user",
    name: str | None = None,
    email: str | None = None,
    profile_image_url: str | None = None,
):
    user = ensure_user(
        id=id,
        role=role,
        name=name,
        email=email,
        profile_image_url=profile_image_url,
    )
    yield user


class AbstractPostgresTest:
    BASE_PATH = ""

    @classmethod
    def setup_class(cls):
        pass

    @classmethod
    def teardown_class(cls):
        pass

    def setup_method(self):
        reset_runtime_state()
        reset_database()
        self.db = SessionLocal()

    def teardown_method(self):
        self.db.close()

    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def make_request(
        self,
        path: str = "/",
        method: str = "GET",
        headers: dict[str, str] | None = None,
        query_string: bytes = b"",
    ) -> Request:
        raw_headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ]
        scope = {
            "type": "http",
            "asgi.version": "3.0",
            "asgi.spec_version": "2.3",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query_string,
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": app,
        }
        request = Request(scope)
        request.state.enable_api_keys = True
        return request

    def make_response(self) -> Response:
        return Response()

    def make_background_tasks(self) -> BackgroundTasks:
        return BackgroundTasks()

    def auth_headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_token({'id': user_id})}"}

    def create_url(self, path: str = "", query_params: dict | None = None) -> str:
        base_path = self.BASE_PATH.rstrip("/")
        suffix = path or ""
        if not suffix:
            suffix = "/"
        elif not suffix.startswith("/"):
            suffix = f"/{suffix}"

        url = f"{base_path}{suffix}"
        if query_params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(query_params, doseq=True)}"

        return url
