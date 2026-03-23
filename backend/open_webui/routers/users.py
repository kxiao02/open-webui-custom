import logging
from typing import Optional
from sqlalchemy.orm import Session
import base64
import io


from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse, FileResponse
from pydantic import BaseModel, ConfigDict


from open_webui.models.auths import Auths
from open_webui.models.oauth_sessions import OAuthSessions

from open_webui.models.groups import Groups
from open_webui.models.chats import Chats
from open_webui.models.users import (
    PRIMARY_ADMIN_INFO_KEY,
    PRIMARY_ADMIN_SET_AT_KEY,
    UserModel,
    UserGroupIdsModel,
    UserGroupIdsListResponse,
    UserInfoResponse,
    UserInfoListResponse,
    UserRoleUpdateForm,
    UserStatus,
    Users,
    UserSettings,
    UserUpdateForm,
)

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import STATIC_DIR
from open_webui.internal.db import get_session


from open_webui.utils.auth import (
    get_admin_user,
    get_password_hash,
    get_verified_user,
    validate_password,
)
from open_webui.utils.access_control import get_permissions, has_permission
from open_webui.utils.models import get_all_models

log = logging.getLogger(__name__)

router = APIRouter()


def _public_user_info(info: Optional[dict]) -> Optional[dict]:
    if not isinstance(info, dict):
        return info

    sanitized = dict(info)
    sanitized.pop(PRIMARY_ADMIN_INFO_KEY, None)
    sanitized.pop(PRIMARY_ADMIN_SET_AT_KEY, None)
    return sanitized or None


def _normalize_model_ids(value) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized_ids = []
    for item in value:
        if not isinstance(item, str):
            continue

        model_id = item.strip()
        if model_id and model_id not in normalized_ids:
            normalized_ids.append(model_id)

    return normalized_ids


def _string_arrays_equal(a: list[str], b: list[str]) -> bool:
    return len(a) == len(b) and all(left == right for left, right in zip(a, b))


async def _reconcile_user_model_settings(
    request: Request, user: UserModel, settings_payload: Optional[dict]
) -> tuple[dict, bool]:
    settings_payload = settings_payload or {}

    ui_settings = settings_payload.get("ui")
    if not isinstance(ui_settings, dict):
        ui_settings = {}

    models = await get_all_models(request, user=user)
    visible_model_ids = [
        model["id"]
        for model in models
        if not ((model.get("info") or {}).get("meta") or {}).get("hidden", False)
    ]

    if len(visible_model_ids) == 0:
        reconciled_settings = {**settings_payload, "ui": ui_settings}
        return reconciled_settings, False

    configured_default_model_ids = [
        model_id
        for model_id in [
            item.strip()
            for item in (request.app.state.config.DEFAULT_MODELS or "").split(",")
        ]
        if model_id and model_id in visible_model_ids
    ]

    original_models = _normalize_model_ids(ui_settings.get("models"))
    original_pinned_models = _normalize_model_ids(ui_settings.get("pinnedModels"))

    current_models = [model_id for model_id in original_models if model_id in visible_model_ids]
    current_pinned_models = [
        model_id for model_id in original_pinned_models if model_id in visible_model_ids
    ]

    next_models = (
        current_models
        or configured_default_model_ids
        or [visible_model_ids[0]]
    )

    models_changed = not _string_arrays_equal(original_models, next_models)
    pinned_models_changed = not _string_arrays_equal(
        original_pinned_models, current_pinned_models
    )

    reconciled_settings = {
        **settings_payload,
        "ui": {
            **ui_settings,
            "models": next_models,
            "pinnedModels": current_pinned_models,
        },
    }

    return reconciled_settings, models_changed or pinned_models_changed


############################
# GetUsers
############################


PAGE_ITEM_COUNT = 30


@router.get("/", response_model=UserGroupIdsListResponse)
async def get_users(
    query: Optional[str] = None,
    order_by: Optional[str] = None,
    direction: Optional[str] = None,
    page: Optional[int] = 1,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    limit = PAGE_ITEM_COUNT

    page = max(1, page)
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter["query"] = query
    if order_by:
        filter["order_by"] = order_by
    if direction:
        filter["direction"] = direction

    filter["direction"] = direction

    result = Users.get_users(filter=filter, skip=skip, limit=limit, db=db)

    users = result["users"]
    total = result["total"]

    # Fetch groups for all users in a single query to avoid N+1
    user_ids = [user.id for user in users]
    user_groups = Groups.get_groups_by_member_ids(user_ids, db=db)

    return {
        "users": [
            UserGroupIdsModel(
                **{
                    **user.model_dump(),
                    "group_ids": [group.id for group in user_groups.get(user.id, [])],
                }
            )
            for user in users
        ],
        "total": total,
    }


@router.get("/all", response_model=UserInfoListResponse)
async def get_all_users(
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    return Users.get_users(db=db)


@router.get("/search", response_model=UserInfoListResponse)
async def search_users(
    query: Optional[str] = None,
    order_by: Optional[str] = None,
    direction: Optional[str] = None,
    page: Optional[int] = 1,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    limit = PAGE_ITEM_COUNT

    page = max(1, page)
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter["query"] = query
    if order_by:
        filter["order_by"] = order_by
    if direction:
        filter["direction"] = direction

    return Users.get_users(filter=filter, skip=skip, limit=limit, db=db)


############################
# User Groups
############################


@router.get("/groups")
async def get_user_groups(
    user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    return Groups.get_groups_by_member_id(user.id, db=db)


############################
# User Permissions
############################


@router.get("/permissions")
async def get_user_permissisions(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    user_permissions = get_permissions(
        user.id, request.app.state.config.USER_PERMISSIONS, db=db
    )

    return user_permissions


############################
# User Default Permissions
############################
class WorkspacePermissions(BaseModel):
    models: bool = False
    knowledge: bool = False
    prompts: bool = False
    tools: bool = False
    skills: bool = False
    models_import: bool = False
    models_export: bool = False
    prompts_import: bool = False
    prompts_export: bool = False
    tools_import: bool = False
    tools_export: bool = False


class SharingPermissions(BaseModel):
    models: bool = False
    public_models: bool = False
    knowledge: bool = False
    public_knowledge: bool = False
    prompts: bool = False
    public_prompts: bool = False
    tools: bool = False
    public_tools: bool = True
    skills: bool = False
    public_skills: bool = False
    notes: bool = False
    public_notes: bool = True


class AccessGrantsPermissions(BaseModel):
    allow_users: bool = True


class ChatPermissions(BaseModel):
    controls: bool = True
    valves: bool = True
    system_prompt: bool = True
    params: bool = True
    file_upload: bool = True
    web_upload: bool = True
    delete: bool = True
    delete_message: bool = True
    continue_response: bool = True
    regenerate_response: bool = True
    rate_response: bool = True
    edit: bool = True
    share: bool = True
    export: bool = True
    stt: bool = True
    tts: bool = True
    call: bool = True
    multiple_models: bool = True
    temporary: bool = True
    temporary_enforced: bool = False


class FeaturesPermissions(BaseModel):
    api_keys: bool = False
    notes: bool = True
    channels: bool = True
    folders: bool = True
    direct_tool_servers: bool = False

    web_search: bool = True
    image_generation: bool = True
    code_interpreter: bool = True
    memories: bool = True


class SettingsPermissions(BaseModel):
    interface: bool = True


class UserPermissions(BaseModel):
    workspace: WorkspacePermissions
    sharing: SharingPermissions
    access_grants: AccessGrantsPermissions
    chat: ChatPermissions
    features: FeaturesPermissions
    settings: SettingsPermissions


@router.get("/default/permissions", response_model=UserPermissions)
async def get_default_user_permissions(request: Request, user=Depends(get_admin_user)):
    return {
        "workspace": WorkspacePermissions(
            **request.app.state.config.USER_PERMISSIONS.get("workspace", {})
        ),
        "sharing": SharingPermissions(
            **request.app.state.config.USER_PERMISSIONS.get("sharing", {})
        ),
        "access_grants": AccessGrantsPermissions(
            **request.app.state.config.USER_PERMISSIONS.get("access_grants", {})
        ),
        "chat": ChatPermissions(
            **request.app.state.config.USER_PERMISSIONS.get("chat", {})
        ),
        "features": FeaturesPermissions(
            **request.app.state.config.USER_PERMISSIONS.get("features", {})
        ),
        "settings": SettingsPermissions(
            **request.app.state.config.USER_PERMISSIONS.get("settings", {})
        ),
    }


@router.post("/default/permissions")
async def update_default_user_permissions(
    request: Request, form_data: UserPermissions, user=Depends(get_admin_user)
):
    request.app.state.config.USER_PERMISSIONS = form_data.model_dump()
    return request.app.state.config.USER_PERMISSIONS


############################
# GetUserSettingsBySessionUser
############################


@router.get("/user/settings", response_model=Optional[UserSettings])
async def get_user_settings_by_session_user(
    request: Request,
    user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    user = Users.get_user_by_id(user.id, db=db)
    if user:
        settings_payload = (
            user.settings.model_dump() if isinstance(user.settings, UserSettings) else user.settings
        )
        reconciled_settings, _changed = await _reconcile_user_model_settings(
            request, user, settings_payload
        )
        return UserSettings(**reconciled_settings)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# UpdateUserSettingsBySessionUser
############################


@router.post("/user/settings/update", response_model=UserSettings)
async def update_user_settings_by_session_user(
    request: Request,
    form_data: UserSettings,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    updated_user_settings = form_data.model_dump()
    ui_settings = updated_user_settings.get("ui")
    if (
        user.role != "admin"
        and ui_settings is not None
        and "toolServers" in ui_settings.keys()
        and not has_permission(
            user.id,
            "features.direct_tool_servers",
            request.app.state.config.USER_PERMISSIONS,
        )
    ):
        # If the user is not an admin and does not have permission to use tool servers, remove the key
        updated_user_settings["ui"].pop("toolServers", None)

    updated_user_settings, _ = await _reconcile_user_model_settings(
        request, user, updated_user_settings
    )

    user = Users.update_user_settings_by_id(user.id, updated_user_settings, db=db)
    if user:
        return user.settings
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# GetUserStatusBySessionUser
############################


@router.get("/user/status")
async def get_user_status_by_session_user(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if not request.app.state.config.ENABLE_USER_STATUS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACTION_PROHIBITED,
        )
    user = Users.get_user_by_id(user.id, db=db)
    if user:
        return user
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# UpdateUserStatusBySessionUser
############################


@router.post("/user/status/update")
async def update_user_status_by_session_user(
    request: Request,
    form_data: UserStatus,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if not request.app.state.config.ENABLE_USER_STATUS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACTION_PROHIBITED,
        )
    user = Users.get_user_by_id(user.id, db=db)
    if user:
        user = Users.update_user_status_by_id(user.id, form_data, db=db)
        return user
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# GetUserInfoBySessionUser
############################


@router.get("/user/info", response_model=Optional[dict])
async def get_user_info_by_session_user(
    user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    user = Users.get_user_by_id(user.id, db=db)
    if user:
        return _public_user_info(user.info)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# UpdateUserInfoBySessionUser
############################


@router.post("/user/info/update", response_model=Optional[dict])
async def update_user_info_by_session_user(
    form_data: dict, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    user = Users.get_user_by_id(user.id, db=db)
    if user:
        if user.info is None:
            user.info = {}

        user = Users.update_user_by_id(
            user.id, {"info": {**user.info, **form_data}}, db=db
        )
        if user:
            return _public_user_info(user.info)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.USER_NOT_FOUND,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# GetUserById
############################


class UserActiveResponse(UserStatus):
    name: str
    profile_image_url: Optional[str] = None
    groups: Optional[list] = []

    is_active: bool
    model_config = ConfigDict(extra="allow")


@router.get("/{user_id}", response_model=UserActiveResponse)
async def get_user_by_id(
    user_id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    # Check if user_id is a shared chat
    # If it is, get the user_id from the chat
    if user_id.startswith("shared-"):
        chat_id = user_id.replace("shared-", "")
        chat = Chats.get_chat_by_id(chat_id)
        if chat:
            user_id = chat.user_id
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.USER_NOT_FOUND,
            )

    user = Users.get_user_by_id(user_id, db=db)
    if user:
        groups = Groups.get_groups_by_member_id(user_id, db=db)
        return UserActiveResponse(
            **{
                **{**user.model_dump(), "info": _public_user_info(user.info)},
                "groups": [{"id": group.id, "name": group.name} for group in groups],
                "is_active": Users.is_user_active(user_id, db=db),
            }
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


@router.get("/{user_id}/info", response_model=UserInfoResponse)
async def get_user_info_by_id(
    user_id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    user = Users.get_user_by_id(user_id, db=db)
    if user:
        groups = Groups.get_groups_by_member_id(user_id, db=db)
        return UserInfoResponse(
            **{
                **{**user.model_dump(), "info": _public_user_info(user.info)},
                "groups": [{"id": group.id, "name": group.name} for group in groups],
                "is_active": Users.is_user_active(user_id, db=db),
            }
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


@router.get("/{user_id}/oauth/sessions")
async def get_user_oauth_sessions_by_id(
    user_id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    sessions = OAuthSessions.get_sessions_by_user_id(user_id, db=db)
    if sessions and len(sessions) > 0:
        return sessions
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# GetUserProfileImageById
############################


@router.get("/{user_id}/profile/image")
def get_user_profile_image_by_id(user_id: str, user=Depends(get_verified_user)):
    user = Users.get_user_by_id(user_id)
    if user:
        if user.profile_image_url:
            # check if it's url or base64
            if user.profile_image_url.startswith("http"):
                return Response(
                    status_code=status.HTTP_302_FOUND,
                    headers={"Location": user.profile_image_url},
                )
            elif user.profile_image_url.startswith("data:image"):
                try:
                    header, base64_data = user.profile_image_url.split(",", 1)
                    image_data = base64.b64decode(base64_data)
                    image_buffer = io.BytesIO(image_data)
                    media_type = header.split(";")[0].lstrip("data:")

                    return StreamingResponse(
                        image_buffer,
                        media_type=media_type,
                        headers={"Content-Disposition": "inline"},
                    )
                except Exception as e:
                    pass
        return FileResponse(f"{STATIC_DIR}/user.png")
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.USER_NOT_FOUND,
        )


############################
# GetUserActiveStatusById
############################


@router.get("/{user_id}/active", response_model=dict)
async def get_user_active_status_by_id(
    user_id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    return {
        "active": Users.is_user_active(user_id, db=db),
    }


############################
# UpdateUserById
############################


@router.post("/{user_id}/update", response_model=Optional[UserModel])
async def update_user_by_id(
    user_id: str,
    form_data: UserUpdateForm,
    session_user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    # Prevent modification of the primary admin user by other admins
    try:
        primary_admin = Users.ensure_primary_admin(db=db)
        if primary_admin:
            if user_id == primary_admin.id:
                if session_user.id != user_id:
                    # If the user trying to update is the primary admin, and they are not the primary admin themselves
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=ERROR_MESSAGES.ACTION_PROHIBITED,
                    )

                if form_data.role != "admin":
                    # If the primary admin is trying to change their own role, prevent it
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=ERROR_MESSAGES.ACTION_PROHIBITED,
                    )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error checking primary admin status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not verify primary admin status.",
        )

    user = Users.get_user_by_id(user_id, db=db)

    if user:
        try:
            if form_data.email.lower() != user.email:
                email_user = Users.get_user_by_email(form_data.email.lower(), db=db)
                if email_user:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=ERROR_MESSAGES.EMAIL_TAKEN,
                    )

            if form_data.password:
                try:
                    validate_password(form_data.password)
                except Exception as e:
                    raise HTTPException(400, detail=str(e))

                hashed = get_password_hash(form_data.password)
                if not Auths.update_user_password_by_id(
                    user_id, hashed, db=db, commit=False
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=ERROR_MESSAGES.DEFAULT(),
                    )

            if not Auths.update_email_by_id(
                user_id, form_data.email.lower(), db=db, commit=False
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(),
                )

            updated_user = Users.update_user_by_id(
                user_id,
                {
                    "role": form_data.role,
                    "name": form_data.name,
                    "profile_image_url": form_data.profile_image_url,
                },
                db=db,
                commit=False,
            )

            if not updated_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(),
                )

            db.commit()

            return Users.get_user_by_id(user_id, db=db)
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(),
            )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(),
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ERROR_MESSAGES.USER_NOT_FOUND,
    )


############################
# DeleteUserById
############################


@router.delete("/{user_id}", response_model=bool)
async def delete_user_by_id(
    user_id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    # Prevent deletion of the primary admin user
    try:
        primary_admin = Users.ensure_primary_admin(db=db)
        if primary_admin and user_id == primary_admin.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.ACTION_PROHIBITED,
            )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error checking primary admin status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not verify primary admin status.",
        )

    if user.id != user_id:
        result = Auths.delete_auth_by_id(user_id, db=db, commit=False)

        if result:
            db.commit()
            return True

        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ERROR_MESSAGES.DELETE_USER_ERROR,
        )

    # Prevent self-deletion
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ERROR_MESSAGES.ACTION_PROHIBITED,
    )


############################
# GetUserGroupsById
############################


@router.get("/{user_id}/groups")
async def get_user_groups_by_id(
    user_id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    return Groups.get_groups_by_member_id(user_id, db=db)
