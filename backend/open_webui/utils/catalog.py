from typing import Any, Iterable

from open_webui.models.access_grants import AccessGrants
from open_webui.models.groups import Groups


VALID_VISIBILITY = {"public", "restricted", "hidden"}


def _meta_value(meta: Any, key: str, default: Any = None) -> Any:
    if meta is None:
        return default
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)


def is_catalog_published(meta: Any) -> bool:
    return bool(_meta_value(meta, "published", False))


def get_catalog_visibility(meta: Any, access_grants: list | None = None) -> str:
    visibility = _meta_value(meta, "visibility", None)
    if visibility in VALID_VISIBILITY:
        return visibility

    return "restricted" if access_grants else "public"


def get_catalog_dependencies(meta: Any) -> list[str]:
    dependencies = _meta_value(meta, "dependencies", [])
    if not isinstance(dependencies, list):
        return []

    return [
        dependency.strip()
        for dependency in dependencies
        if isinstance(dependency, str) and dependency.strip()
    ]


def get_user_group_ids(user_id: str, db=None) -> set[str]:
    return {group.id for group in Groups.get_groups_by_member_id(user_id, db=db)}


def _normalize_resource_ids(resource_ids: Iterable[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for resource_id in resource_ids or []:
        if not isinstance(resource_id, str):
            continue

        value = resource_id.strip()
        if not value or value in seen:
            continue

        seen.add(value)
        normalized.append(value)

    return normalized


def is_catalog_runtime_activatable(meta: Any, access_grants: list | None = None) -> bool:
    # Runtime availability is decided by the caller's visibility/access checks.
    # Hidden or unpublished personal drafts still need to remain selectable.
    return True


def filter_hidden_tool_ids(tool_ids: Iterable[Any] | None, db=None) -> list[str]:
    return _normalize_resource_ids(tool_ids)


def filter_hidden_skill_ids(skill_ids: Iterable[Any] | None, db=None) -> list[str]:
    return _normalize_resource_ids(skill_ids)


def is_tool_catalog_visible(tool: Any, user: Any, user_group_ids: set[str], db=None) -> bool:
    if getattr(user, "role", None) == "admin":
        return True

    if getattr(tool, "user_id", None) == getattr(user, "id", None):
        return True

    if not is_catalog_published(getattr(tool, "meta", None)):
        return False

    visibility = get_catalog_visibility(
        getattr(tool, "meta", None), getattr(tool, "access_grants", [])
    )
    if visibility == "hidden":
        return False
    if visibility == "public":
        return True

    return AccessGrants.has_access(
        user_id=user.id,
        resource_type="tool",
        resource_id=tool.id,
        permission="read",
        user_group_ids=user_group_ids,
        db=db,
    )


def is_skill_catalog_visible(
    skill: Any,
    user: Any,
    user_group_ids: set[str],
    db=None,
    require_active: bool = True,
) -> bool:
    if getattr(user, "role", None) == "admin":
        return True

    if getattr(skill, "user_id", None) == getattr(user, "id", None):
        return True

    if require_active and not getattr(skill, "is_active", False):
        return False

    if not is_catalog_published(getattr(skill, "meta", None)):
        return False

    visibility = get_catalog_visibility(
        getattr(skill, "meta", None), getattr(skill, "access_grants", [])
    )
    if visibility == "hidden":
        return False
    if visibility == "public":
        return True

    return AccessGrants.has_access(
        user_id=user.id,
        resource_type="skill",
        resource_id=skill.id,
        permission="read",
        user_group_ids=user_group_ids,
        db=db,
    )


def filter_visible_tools(tools: Iterable[Any], user: Any, db=None) -> list[Any]:
    user_group_ids = (
        set() if getattr(user, "role", None) == "admin" else get_user_group_ids(user.id, db=db)
    )
    return [
        tool for tool in tools if is_tool_catalog_visible(tool, user, user_group_ids, db=db)
    ]


def filter_visible_skills(
    skills: Iterable[Any], user: Any, db=None, require_active: bool = True
) -> list[Any]:
    user_group_ids = (
        set() if getattr(user, "role", None) == "admin" else get_user_group_ids(user.id, db=db)
    )
    return [
        skill
        for skill in skills
        if is_skill_catalog_visible(
            skill, user, user_group_ids, db=db, require_active=require_active
        )
    ]
