import logging
from typing import Optional

from open_webui.models.groups import Groups
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from open_webui.internal.db import get_session
from open_webui.models.skills import (
    SkillForm,
    SkillModel,
    SkillResponse,
    SkillUserResponse,
    SkillAccessResponse,
    SkillAccessListResponse,
    Skills,
)
from open_webui.models.access_grants import AccessGrants
from open_webui.models.resource_installations import ResourceInstallations
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_permission, filter_allowed_access_grants
from open_webui.utils.catalog import (
    filter_visible_skills,
    get_user_group_ids,
    is_skill_catalog_visible,
)

from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES

log = logging.getLogger(__name__)

PAGE_ITEM_COUNT = 30

router = APIRouter()


def _skill_write_access(user) -> bool:
    return user.role == "admin"


def _get_installed_skill_ids(
    user_id: str,
    skill_ids: Optional[list[str] | set[str] | tuple[str, ...]] = None,
    db=None,
) -> set[str]:
    return ResourceInstallations.get_installed_resource_ids(
        user_id, "skill", resource_ids=skill_ids, db=db
    )


############################
# GetSkills
############################


@router.get("/", response_model=list[SkillUserResponse])
async def get_skills(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    visible_skills = filter_visible_skills(Skills.get_skills(db=db), user, db=db)
    installed_skill_ids = _get_installed_skill_ids(
        user.id, [skill.id for skill in visible_skills], db=db
    )
    return [
        SkillUserResponse(
            **{
                **skill.model_dump(),
                "installed": skill.id in installed_skill_ids,
            }
        )
        for skill in visible_skills
    ]


############################
# GetSkillList
############################


@router.get("/list", response_model=SkillAccessListResponse)
async def get_skill_list(
    query: Optional[str] = None,
    view_option: Optional[str] = None,
    page: Optional[int] = 1,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    filtered_skills = filter_visible_skills(Skills.get_skills(db=db), user, db=db)
    installed_skill_ids = _get_installed_skill_ids(
        user.id, [skill.id for skill in filtered_skills], db=db
    )

    normalized_query = (query or "").strip().lower()
    if normalized_query:
        filtered_skills = [
            skill
            for skill in filtered_skills
            if normalized_query in (skill.name or "").lower()
            or normalized_query in (skill.description or "").lower()
            or normalized_query in (skill.id or "").lower()
            or normalized_query in ((skill.user.name if skill.user else "") or "").lower()
            or normalized_query in ((skill.user.email if skill.user else "") or "").lower()
        ]

    if view_option == "created":
        filtered_skills = [skill for skill in filtered_skills if skill.user_id == user.id]
    elif view_option == "shared":
        filtered_skills = [skill for skill in filtered_skills if skill.user_id != user.id]

    limit = PAGE_ITEM_COUNT
    page = max(1, page)
    skip = (page - 1) * limit
    items = filtered_skills[skip : skip + limit]

    return SkillAccessListResponse(
        items=[
            SkillAccessResponse(
                **skill.model_dump(),
                write_access=_skill_write_access(user),
                installed=skill.id in installed_skill_ids,
            )
            for skill in items
        ],
        total=len(filtered_skills),
    )


############################
# ExportSkills
############################


@router.get("/export", response_model=list[SkillModel])
async def export_skills(
    request: Request,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    return Skills.get_skills(db=db)


############################
# CreateNewSkill
############################


@router.post("/create", response_model=Optional[SkillResponse])
async def create_new_skill(
    request: Request,
    form_data: SkillForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    form_data.id = form_data.id.lower().replace(" ", "-")

    existing = Skills.get_skill_by_id(form_data.id, db=db)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ID_TAKEN,
        )

    try:
        skill = Skills.insert_new_skill(user.id, form_data, db=db)
        if skill:
            return skill
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error creating skill"),
            )
    except Exception as e:
        log.exception(f"Failed to create skill: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# GetSkillById
############################


@router.get("/id/{id}", response_model=Optional[SkillAccessResponse])
async def get_skill_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    skill = Skills.get_skill_by_id(id, db=db)

    if skill:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if is_skill_catalog_visible(skill, user, user_group_ids, db=db):
            return SkillAccessResponse(
                **skill.model_dump(),
                write_access=_skill_write_access(user),
                installed=id in _get_installed_skill_ids(user.id, [id], db=db),
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


############################
# InstallSkillById
############################


@router.post("/id/{id}/install", response_model=dict)
async def install_skill_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    skill = Skills.get_skill_by_id(id, db=db)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
    if not is_skill_catalog_visible(skill, user, user_group_ids, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    ResourceInstallations.install_resource(user.id, "skill", id, db=db)
    return {"id": id, "installed": True}


@router.delete("/id/{id}/install", response_model=dict)
async def uninstall_skill_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    ResourceInstallations.uninstall_resource(user.id, "skill", id, db=db)
    return {"id": id, "installed": False}


############################
# UpdateSkillById
############################


@router.post("/id/{id}/update", response_model=Optional[SkillModel])
async def update_skill_by_id(
    request: Request,
    id: str,
    form_data: SkillForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    skill = Skills.get_skill_by_id(id, db=db)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    try:
        updated = {
            **form_data.model_dump(exclude={"id"}),
        }

        skill = Skills.update_skill_by_id(id, updated, db=db)

        if skill:
            return skill
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating skill"),
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# UpdateSkillAccessById
############################


class SkillAccessGrantsForm(BaseModel):
    access_grants: list[dict]


@router.post("/id/{id}/access/update", response_model=Optional[SkillModel])
async def update_skill_access_by_id(
    request: Request,
    id: str,
    form_data: SkillAccessGrantsForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    skill = Skills.get_skill_by_id(id, db=db)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    form_data.access_grants = filter_allowed_access_grants(
        request.app.state.config.USER_PERMISSIONS,
        user.id,
        user.role,
        form_data.access_grants,
        "sharing.public_skills",
    )

    AccessGrants.set_access_grants("skill", id, form_data.access_grants, db=db)

    return Skills.get_skill_by_id(id, db=db)


############################
# ToggleSkillById
############################


@router.post("/id/{id}/toggle", response_model=Optional[SkillModel])
async def toggle_skill_by_id(
    id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    skill = Skills.get_skill_by_id(id, db=db)
    if skill:
        skill = Skills.toggle_skill_by_id(id, db=db)

        if skill:
            return skill
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error toggling skill"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# DeleteSkillById
############################


@router.delete("/id/{id}/delete", response_model=bool)
async def delete_skill_by_id(
    request: Request,
    id: str,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    skill = Skills.get_skill_by_id(id, db=db)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    result = Skills.delete_skill_by_id(id, db=db)
    return result
