from typing import Optional
import io
import base64
import hashlib
import json
import asyncio
import logging

from open_webui.models.models import (
    ModelForm,
    ModelMeta,
    ModelModel,
    ModelParams,
    ModelResponse,
    ModelListResponse,
    ModelAccessListResponse,
    ModelAccessResponse,
    Models,
)
from open_webui.models.access_grants import AccessGrants

from pydantic import BaseModel
from open_webui.constants import ERROR_MESSAGES
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
    Response,
)
from fastapi.responses import FileResponse, StreamingResponse


from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_permission, filter_allowed_access_grants
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL, STATIC_DIR
from open_webui.internal.db import get_session
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

router = APIRouter()

_LEGACY_DEFAULT_PROFILE_IMAGE_URLS = {
    "/favicon.png",
    "/static/favicon.png",
    "/static/favicon-dark.png",
    "/static/favicon-96x96.png",
    "/static/apple-touch-icon.png",
    "/static/logo.png",
    "/static/favicon.svg",
    "/static/favicon.ico",
    "https://openwebui.com/favicon.png",
}

# Hashes for historical built-in Open WebUI icon binaries.
_LEGACY_DEFAULT_PROFILE_IMAGE_SHA256 = {
    "443e444984c0df30efa5d649cc9c1bc73a81aa01b9ec8e75f1c22dd245c89732",  # favicon.png (+ embedded favicon.svg PNG)
    "5be61c5b4742e43edc1fba9c19c80ea83e48c10cfcffbc699dc40f62a52d3ad0",  # favicon-dark.png
    "159e33435208b49d10a7a54ba01539314b026d61469e4d6c3caf31ca9a0cc95c",  # favicon-96x96.png
    "31bec935966104f4403243981687013ea246c3315a902da967f4970700eddb18",  # logo.png
    "5f03e55a378426d5fa7593503d3cf051b3d8bb1c7861650f7e78419e5a71c731",  # apple-touch-icon.png
    "ffdc414e31dc08d0f9f85b49d6f356b9eb0fd1438af21bacc3d7957ee8bd0df4",  # web-app-manifest-192x192.png
    "01b8f5cc95d4a2991bab0d854f71ace436160ec9416a8b31894b8dd68b8a7b9e",  # web-app-manifest-512x512.png
}


def _normalize_profile_image_url(value: str) -> str:
    return value.strip().split("?", 1)[0].split("#", 1)[0].lower()


def _is_legacy_default_profile_image_url(value: str) -> bool:
    return _normalize_profile_image_url(value) in _LEGACY_DEFAULT_PROFILE_IMAGE_URLS


def _decode_data_image(value: str) -> Optional[tuple[bytes, str]]:
    if not value.startswith("data:image"):
        return None

    try:
        header, base64_data = value.split(",", 1)
        return base64.b64decode(base64_data), header.split(";")[0].lstrip("data:")
    except Exception:
        return None


def _profile_image_cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def _is_dark_theme(theme: Optional[str]) -> bool:
    return (theme or "").strip().lower() in {"dark", "true", "1"}


def _get_fallback_profile_image_path(theme: Optional[str]) -> str:
    if _is_dark_theme(theme):
        dark_icon_path = STATIC_DIR / "favicon-dark.png"
        if dark_icon_path.exists():
            return str(dark_icon_path)
    return str(STATIC_DIR / "favicon.png")


def is_valid_model_id(model_id: str) -> bool:
    return model_id and len(model_id) <= 256


def _can_access_workspace_content(user, owner_user_id: str) -> bool:
    # Models are shared workspace resources in this deployment.
    # Any verified user can read and manage model entries.
    return True


###########################
# GetModels
###########################


PAGE_ITEM_COUNT = 30


@router.get(
    "/list", response_model=ModelAccessListResponse
)  # do NOT use "/" as path, conflicts with main.py
async def get_models(
    query: Optional[str] = None,
    view_option: Optional[str] = None,
    tag: Optional[str] = None,
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
    if view_option:
        filter["view_option"] = view_option
    if tag:
        filter["tag"] = tag
    if order_by:
        filter["order_by"] = order_by
    if direction:
        filter["direction"] = direction

    # Pre-fetch user group IDs once - used for both filter and write_access check
    groups = Groups.get_groups_by_member_id(user.id, db=db)
    user_group_ids = {group.id for group in groups}

    if not user.role == "admin" or not BYPASS_ADMIN_ACCESS_CONTROL:
        if groups:
            filter["group_ids"] = [group.id for group in groups]

        filter["user_id"] = user.id

    result = Models.search_models(user.id, filter=filter, skip=skip, limit=limit, db=db)

    # Batch-fetch writable model IDs in a single query instead of N has_access calls
    model_ids = [model.id for model in result.items]
    writable_model_ids = AccessGrants.get_accessible_resource_ids(
        user_id=user.id,
        resource_type="model",
        resource_ids=model_ids,
        permission="write",
        user_group_ids=user_group_ids,
        db=db,
    )

    return ModelAccessListResponse(
        items=[
            ModelAccessResponse(
                **model.model_dump(),
                write_access=(
                    (user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == model.user_id
                    or model.id in writable_model_ids
                ),
            )
            for model in result.items
        ],
        total=result.total,
    )


###########################
# GetBaseModels
###########################


@router.get("/base", response_model=list[ModelResponse])
async def get_base_models(
    user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    return Models.get_base_models(db=db)


###########################
# GetModelTags
###########################


@router.get("/tags", response_model=list[str])
async def get_model_tags(
    user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    models = Models.get_models(db=db)

    tags_set = set()
    for model in models:
        if model.meta:
            meta = model.meta.model_dump()
            for tag in meta.get("tags", []):
                tags_set.add((tag.get("name")))

    tags = [tag for tag in tags_set]
    tags.sort()
    return tags


############################
# CreateNewModel
############################


@router.post("/create", response_model=Optional[ModelModel])
async def create_new_model(
    form_data: ModelForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    model = Models.get_model_by_id(form_data.id, db=db)
    if model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.MODEL_ID_TAKEN,
        )

    if not is_valid_model_id(form_data.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.MODEL_ID_TOO_LONG,
        )

    else:
        model = Models.insert_new_model(form_data, user.id, db=db)
        if model:
            return model
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.DEFAULT(),
            )


############################
# ExportModels
############################


@router.get("/export", response_model=list[ModelModel])
async def export_models(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin" and not has_permission(
        user.id,
        "workspace.models_export",
        request.app.state.config.USER_PERMISSIONS,
        db=db,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    if user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL:
        return Models.get_models(db=db)
    else:
        return Models.get_models_by_user_id(user.id, db=db)


############################
# ImportModels
############################


class ModelsImportForm(BaseModel):
    models: list[dict]


@router.post("/import", response_model=bool)
async def import_models(
    request: Request,
    user=Depends(get_verified_user),
    form_data: ModelsImportForm = (...),
    db: Session = Depends(get_session),
):
    if user.role != "admin" and not has_permission(
        user.id,
        "workspace.models_import",
        request.app.state.config.USER_PERMISSIONS,
        db=db,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    try:
        data = form_data.models
        if isinstance(data, list):
            # Batch-fetch all existing models in one query to avoid N+1
            model_ids = [
                model_data.get("id")
                for model_data in data
                if model_data.get("id") and is_valid_model_id(model_data.get("id"))
            ]
            existing_models = {
                model.id: model
                for model in (
                    Models.get_models_by_ids(model_ids, db=db) if model_ids else []
                )
            }

            for model_data in data:
                # Here, you can add logic to validate model_data if needed
                model_id = model_data.get("id")

                if model_id and is_valid_model_id(model_id):
                    existing_model = existing_models.get(model_id)
                    if existing_model:
                        # Update existing model
                        model_data["meta"] = model_data.get("meta", {})
                        model_data["params"] = model_data.get("params", {})

                        updated_model = ModelForm(
                            **{**existing_model.model_dump(), **model_data}
                        )
                        Models.update_model_by_id(model_id, updated_model, db=db)
                    else:
                        # Insert new model
                        model_data["meta"] = model_data.get("meta", {})
                        model_data["params"] = model_data.get("params", {})
                        new_model = ModelForm(**model_data)
                        Models.insert_new_model(
                            user_id=user.id, form_data=new_model, db=db
                        )
            return True
        else:
            raise HTTPException(status_code=400, detail="Invalid JSON format")
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


############################
# SyncModels
############################


class SyncModelsForm(BaseModel):
    models: list[ModelModel] = []


@router.post("/sync", response_model=list[ModelModel])
async def sync_models(
    request: Request,
    form_data: SyncModelsForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    return Models.sync_models(user.id, form_data.models, db=db)


###########################
# GetModelById
###########################


class ModelIdForm(BaseModel):
    id: str


# Note: We're not using the typical url path param here, but instead using a query parameter to allow '/' in the id
@router.get("/model", response_model=Optional[ModelAccessResponse])
async def get_model_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    model = Models.get_model_by_id(id, db=db)
    if model:
        if (
            (user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL)
            or model.user_id == user.id
            or AccessGrants.has_access(
                user_id=user.id,
                resource_type="model",
                resource_id=model.id,
                permission="read",
                db=db,
            )
        ):
            return ModelAccessResponse(
                **model.model_dump(),
                write_access=(
                    (user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == model.user_id
                    or AccessGrants.has_access(
                        user_id=user.id,
                        resource_type="model",
                        resource_id=model.id,
                        permission="write",
                        db=db,
                    )
                ),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


###########################
# GetModelById
###########################


@router.get("/model/profile/image")
def get_model_profile_image(
    id: str,
    theme: Optional[str] = None,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    model = Models.get_model_by_id(id, db=db)
    fallback_image_path = _get_fallback_profile_image_path(theme)

    if model:
        if not _can_access_workspace_content(user, model.user_id):
            return FileResponse(
                fallback_image_path, headers=_profile_image_cache_headers()
            )
        etag = f'"{model.updated_at}"' if model.updated_at else None

        profile_image_url = model.meta.profile_image_url
        if profile_image_url:
            if _is_legacy_default_profile_image_url(profile_image_url):
                return FileResponse(
                    fallback_image_path, headers=_profile_image_cache_headers()
                )

            if profile_image_url.startswith("http"):
                return Response(
                    status_code=status.HTTP_302_FOUND,
                    headers={
                        **_profile_image_cache_headers(),
                        "Location": profile_image_url,
                    },
                )

            decoded = _decode_data_image(profile_image_url)
            if decoded:
                image_data, media_type = decoded
                image_hash = hashlib.sha256(image_data).hexdigest()

                # If model metadata still holds a legacy built-in icon blob, serve current favicon.
                if image_hash in _LEGACY_DEFAULT_PROFILE_IMAGE_SHA256:
                    return FileResponse(
                        fallback_image_path,
                        headers=_profile_image_cache_headers(),
                    )

                image_buffer = io.BytesIO(image_data)
                headers = {
                    "Content-Disposition": "inline",
                    **_profile_image_cache_headers(),
                }
                if etag:
                    headers["ETag"] = etag

                return StreamingResponse(
                    image_buffer,
                    media_type=media_type,
                    headers=headers,
                )

        return FileResponse(fallback_image_path, headers=_profile_image_cache_headers())
    else:
        return FileResponse(fallback_image_path, headers=_profile_image_cache_headers())


############################
# ToggleModelById
############################


@router.post("/model/toggle", response_model=Optional[ModelResponse])
async def toggle_model_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    model = Models.get_model_by_id(id, db=db)
    if model:
        if (
            user.role == "admin"
            or model.user_id == user.id
            or AccessGrants.has_access(
                user_id=user.id,
                resource_type="model",
                resource_id=model.id,
                permission="write",
                db=db,
            )
        ):
            model = Models.toggle_model_by_id(id, db=db)

            if model:
                return model
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error updating function"),
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# UpdateModelById
############################


@router.post("/model/update", response_model=Optional[ModelModel])
async def update_model_by_id(
    form_data: ModelForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    model = Models.get_model_by_id(form_data.id, db=db)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        model.user_id != user.id
        and not AccessGrants.has_access(
            user_id=user.id,
            resource_type="model",
            resource_id=model.id,
            permission="write",
            db=db,
        )
        and user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    model = Models.update_model_by_id(
        form_data.id, ModelForm(**form_data.model_dump()), db=db
    )
    return model


############################
# UpdateModelAccessById
############################


class ModelAccessGrantsForm(BaseModel):
    id: str
    name: Optional[str] = None
    access_grants: list[dict]


@router.post("/model/access/update", response_model=Optional[ModelModel])
async def update_model_access_by_id(
    request: Request,
    form_data: ModelAccessGrantsForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    model = Models.get_model_by_id(form_data.id, db=db)

    # Non-preset models (e.g. direct Ollama/OpenAI models) may not have a DB
    # entry yet. Create a minimal one so access grants can be stored.
    if not model:
        if user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        model = Models.insert_new_model(
            ModelForm(
                id=form_data.id,
                name=form_data.name or form_data.id,
                meta=ModelMeta(),
                params=ModelParams(),
            ),
            user.id,
            db=db,
        )
        if not model:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT("Error creating model entry"),
            )

    if (
        model.user_id != user.id
        and not AccessGrants.has_access(
            user_id=user.id,
            resource_type="model",
            resource_id=model.id,
            permission="write",
            db=db,
        )
        and user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    form_data.access_grants = filter_allowed_access_grants(
        request.app.state.config.USER_PERMISSIONS,
        user.id,
        user.role,
        form_data.access_grants,
        "sharing.public_models",
    )

    AccessGrants.set_access_grants(
        "model", form_data.id, form_data.access_grants, db=db
    )

    return Models.get_model_by_id(form_data.id, db=db)


############################
# DeleteModelById
############################


@router.post("/model/delete", response_model=bool)
async def delete_model_by_id(
    form_data: ModelIdForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    model = Models.get_model_by_id(form_data.id, db=db)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        user.role != "admin"
        and model.user_id != user.id
        and not AccessGrants.has_access(
            user_id=user.id,
            resource_type="model",
            resource_id=model.id,
            permission="write",
            db=db,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    result = Models.delete_model_by_id(form_data.id, db=db)
    return result


@router.delete("/delete/all", response_model=bool)
async def delete_all_models(
    user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    result = Models.delete_all_models(db=db)
    return result
