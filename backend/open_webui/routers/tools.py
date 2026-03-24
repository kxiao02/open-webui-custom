import logging
from pathlib import Path
from typing import Optional
import time
import re
import aiohttp
from open_webui.env import AIOHTTP_CLIENT_TIMEOUT
from open_webui.models.groups import Groups
from pydantic import BaseModel, Field, HttpUrl
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from open_webui.internal.db import get_session


from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.tools import (
    ToolForm,
    ToolModel,
    ToolResponse,
    ToolUserResponse,
    ToolAccessResponse,
    Tools,
)
from open_webui.models.access_grants import AccessGrants
from open_webui.models.resource_installations import ResourceInstallations
from open_webui.utils.plugin import (
    load_tool_module_by_id,
    replace_imports,
    get_tool_module_from_cache,
    resolve_valves_schema_options,
)
from open_webui.utils.tools import get_builtin_tool_catalog, get_tool_specs
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import (
    has_permission,
    has_access,
    filter_allowed_access_grants,
)
from open_webui.utils.tools import get_tool_servers
from open_webui.utils.catalog import (
    filter_visible_tools,
    get_user_group_ids,
    is_tool_catalog_visible,
)

from open_webui.config import CACHE_DIR, BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES

log = logging.getLogger(__name__)


router = APIRouter()


class BuiltinToolCatalogMeta(BaseModel):
    description: str = ""
    category: str = "builtin"
    origin: str = "builtin"
    mutability: str = "locked"
    default_enabled: bool = True
    available: bool = True
    capability_requirements: list[str] = Field(default_factory=list)
    feature_requirements: list[str] = Field(default_factory=list)
    config_requirements: list[str] = Field(default_factory=list)


class BuiltinToolCatalogResponse(BaseModel):
    id: str
    name: str
    meta: BuiltinToolCatalogMeta


def get_tool_module(request, tool_id, load_from_db=True):
    """
    Get the tool module by its ID.
    """
    tool_module, _ = get_tool_module_from_cache(request, tool_id, load_from_db)
    return tool_module


def _can_access_workspace_content(user, owner_user_id: str) -> bool:
    return owner_user_id == user.id or (
        user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL
    )


def _is_default_tool(tool) -> bool:
    if not tool:
        return False
    meta = getattr(tool, "meta", None)
    if meta is None:
        return False
    if hasattr(meta, "is_default"):
        return bool(meta.is_default)
    if isinstance(meta, dict):
        return bool(meta.get("is_default", False))
    return False


def _tool_write_access(
    user, tool, user_group_ids: Optional[set[str]] = None, db: Session | None = None
) -> bool:
    if not tool:
        return False
    if _is_default_tool(tool):
        return False
    if user.role == "admin":
        return True
    if tool.user_id == user.id:
        return True
    if user_group_ids is None:
        user_group_ids = get_user_group_ids(user.id, db=db)
    return AccessGrants.has_access(
        user_id=user.id,
        resource_type="tool",
        resource_id=tool.id,
        permission="write",
        user_group_ids=user_group_ids,
        db=db,
    )


def _get_installed_tool_ids(
    user_id: str, tool_ids: Optional[list[str] | set[str] | tuple[str, ...]] = None, db=None
) -> set[str]:
    return ResourceInstallations.get_installed_resource_ids(
        user_id, "tool", resource_ids=tool_ids, db=db
    )


async def _get_server_tool_entries(request: Request, user, db=None):
    server_tools: list[dict] = []
    server_access_grants: dict[str, list] = {}

    # OpenAPI Tool Servers
    for server in await get_tool_servers(request):
        connection = request.app.state.config.TOOL_SERVER_CONNECTIONS[
            server.get("idx", 0)
        ]
        server_config = connection.get("config", {})

        server_id = f"server:{server.get('id')}"
        server_access_grants[server_id] = server_config.get("access_grants", [])

        server_tools.append(
            {
                "id": server_id,
                "user_id": server_id,
                "name": server.get("openapi", {})
                .get("info", {})
                .get("title", "Tool Server"),
                "meta": {
                    "description": server.get("openapi", {})
                    .get("info", {})
                    .get("description", ""),
                },
                "access_grants": [],
                "updated_at": int(time.time()),
                "created_at": int(time.time()),
            }
        )

    # MCP Tool Servers
    for server in request.app.state.config.TOOL_SERVER_CONNECTIONS:
        if server.get("type", "openapi") == "mcp" and server.get("config", {}).get(
            "enable"
        ):
            server_id = server.get("info", {}).get("id")
            auth_type = server.get("auth_type", "none")

            session_token = None
            if auth_type == "oauth_2.1":
                splits = server_id.split(":")
                server_id = splits[-1] if len(splits) > 1 else server_id

                session_token = (
                    await request.app.state.oauth_client_manager.get_oauth_token(
                        user.id, f"mcp:{server_id}"
                    )
                )

            server_config = server.get("config", {})

            tool_id = f"server:mcp:{server.get('info', {}).get('id')}"
            server_access_grants[tool_id] = server_config.get("access_grants", [])

            entry = {
                "id": tool_id,
                "user_id": tool_id,
                "name": server.get("info", {}).get("name", "MCP Tool Server"),
                "meta": {
                    "description": server.get("info", {}).get("description", ""),
                },
                "access_grants": [],
                "updated_at": int(time.time()),
                "created_at": int(time.time()),
            }
            if auth_type == "oauth_2.1":
                entry["authenticated"] = session_token is not None

            server_tools.append(entry)

    return server_tools, server_access_grants


async def _check_server_tool_access(
    request: Request, user, tool_id: str, db=None
) -> tuple[bool, bool]:
    user_group_ids = (
        set() if getattr(user, "role", None) == "admin" else get_user_group_ids(user.id, db=db)
    )

    if tool_id.startswith("server:mcp:"):
        for server in request.app.state.config.TOOL_SERVER_CONNECTIONS:
            if server.get("type", "openapi") != "mcp":
                continue
            if not server.get("config", {}).get("enable"):
                continue
            candidate_id = f"server:mcp:{server.get('info', {}).get('id')}"
            if candidate_id != tool_id:
                continue
            access_grants = server.get("config", {}).get("access_grants", [])
            if getattr(user, "role", None) == "admin":
                return True, True
            return True, has_access(
                user.id, "read", access_grants, user_group_ids, db=db
            )
        return False, False

    if tool_id.startswith("server:"):
        for server in await get_tool_servers(request):
            candidate_id = f"server:{server.get('id')}"
            if candidate_id != tool_id:
                continue
            connection = request.app.state.config.TOOL_SERVER_CONNECTIONS[
                server.get("idx", 0)
            ]
            access_grants = connection.get("config", {}).get("access_grants", [])
            if getattr(user, "role", None) == "admin":
                return True, True
            return True, has_access(
                user.id, "read", access_grants, user_group_ids, db=db
            )
        return False, False

    return False, False


############################
# GetTools
############################


@router.get("/", response_model=list[ToolUserResponse])
async def get_tools(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = []
    installed_tool_ids = _get_installed_tool_ids(user.id, db=db)

    # Local Tools
    for tool in Tools.get_tools(defer_content=True, db=db):
        tool_module = (
            request.app.state.TOOLS.get(tool.id)
            if hasattr(request.app.state, "TOOLS")
            else None
        )
        tools.append(
            ToolUserResponse(
                **{
                    **tool.model_dump(),
                    "has_user_valves": (
                        hasattr(tool_module, "UserValves") if tool_module else False
                    ),
                    "installed": tool.id in installed_tool_ids,
                }
            )
        )

    # OpenAPI Tool Servers
    server_access_grants = {}
    for server in await get_tool_servers(request):
        connection = request.app.state.config.TOOL_SERVER_CONNECTIONS[
            server.get("idx", 0)
        ]
        server_config = connection.get("config", {})

        server_id = f"server:{server.get('id')}"
        server_access_grants[server_id] = server_config.get("access_grants", [])

        tools.append(
            ToolUserResponse(
                **{
                    "id": server_id,
                    "user_id": server_id,
                    "name": server.get("openapi", {})
                    .get("info", {})
                    .get("title", "Tool Server"),
                    "meta": {
                        "description": server.get("openapi", {})
                        .get("info", {})
                        .get("description", ""),
                    },
                    "updated_at": int(time.time()),
                    "created_at": int(time.time()),
                }
            )
        )

    # MCP Tool Servers
    for server in request.app.state.config.TOOL_SERVER_CONNECTIONS:
        if server.get("type", "openapi") == "mcp" and server.get("config", {}).get(
            "enable"
        ):
            server_id = server.get("info", {}).get("id")
            auth_type = server.get("auth_type", "none")

            session_token = None
            if auth_type == "oauth_2.1":
                splits = server_id.split(":")
                server_id = splits[-1] if len(splits) > 1 else server_id

                session_token = (
                    await request.app.state.oauth_client_manager.get_oauth_token(
                        user.id, f"mcp:{server_id}"
                    )
                )

            server_config = server.get("config", {})

            tool_id = f"server:mcp:{server.get('info', {}).get('id')}"
            server_access_grants[tool_id] = server_config.get("access_grants", [])

            tools.append(
                ToolUserResponse(
                    **{
                        "id": tool_id,
                        "user_id": tool_id,
                        "name": server.get("info", {}).get("name", "MCP Tool Server"),
                        "meta": {
                            "description": server.get("info", {}).get(
                                "description", ""
                            ),
                        },
                        "updated_at": int(time.time()),
                        "created_at": int(time.time()),
                        **(
                            {
                                "authenticated": session_token is not None,
                            }
                            if auth_type == "oauth_2.1"
                            else {}
                        ),
                    }
                )
            )

    if user.role == "admin":
        return tools

    user_group_ids = get_user_group_ids(user.id, db=db)
    return [
        tool
        for tool in tools
        if (
            has_access(
                user.id,
                "read",
                server_access_grants.get(str(tool.id), []),
                user_group_ids,
                db=db,
            )
            if str(tool.id).startswith("server:")
            else is_tool_catalog_visible(tool, user, user_group_ids, db=db)
        )
    ]


############################
# GetToolList
############################


@router.get("/list", response_model=list[ToolAccessResponse])
async def get_tool_list(
    request: Request,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    db_tools = filter_visible_tools(Tools.get_tools(defer_content=True, db=db), user, db=db)
    server_tools, server_access_grants = await _get_server_tool_entries(request, user, db=db)

    if user.role != "admin":
        user_group_ids = get_user_group_ids(user.id, db=db)
        server_tools = [
            tool
            for tool in server_tools
            if has_access(
                user.id,
                "read",
                server_access_grants.get(str(tool.get("id")), []),
                user_group_ids,
                db=db,
            )
        ]

    all_ids = [tool.id for tool in db_tools] + [tool.get("id") for tool in server_tools]
    installed_tool_ids = _get_installed_tool_ids(user.id, all_ids, db=db)

    result: list[ToolAccessResponse] = []
    for tool in db_tools:
        result.append(
            ToolAccessResponse(
                **tool.model_dump(),
                write_access=_tool_write_access(user, tool, db=db),
                installed=tool.id in installed_tool_ids,
            )
        )
    for tool in server_tools:
        result.append(
            ToolAccessResponse(
                **tool,
                write_access=False,
                installed=tool.get("id") in installed_tool_ids,
            )
        )

    return result


@router.get("/builtin/list", response_model=list[BuiltinToolCatalogResponse])
async def get_builtin_tool_list(
    request: Request,
    user=Depends(get_verified_user),
):
    del user
    return get_builtin_tool_catalog(request)


############################
# LoadFunctionFromLink
############################


class LoadUrlForm(BaseModel):
    url: HttpUrl


def github_url_to_raw_url(url: str) -> str:
    # Handle 'tree' (folder) URLs (add main.py at the end)
    m1 = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)", url)
    if m1:
        org, repo, branch, path = m1.groups()
        return f"https://raw.githubusercontent.com/{org}/{repo}/refs/heads/{branch}/{path.rstrip('/')}/main.py"

    # Handle 'blob' (file) URLs
    m2 = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)", url)
    if m2:
        org, repo, branch, path = m2.groups()
        return (
            f"https://raw.githubusercontent.com/{org}/{repo}/refs/heads/{branch}/{path}"
        )

    # No match; return as-is
    return url


@router.post("/load/url", response_model=Optional[dict])
async def load_tool_from_url(
    request: Request, form_data: LoadUrlForm, user=Depends(get_admin_user)
):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tool import from URL is disabled.",
    )


############################
# ExportTools
############################


@router.get("/export", response_model=list[ToolModel])
async def export_tools(
    request: Request,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    return Tools.get_tools(db=db)


############################
# CreateNewTools
############################


@router.post("/create", response_model=Optional[ToolResponse])
async def create_new_tools(
    request: Request,
    form_data: ToolForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if user.role != "admin" and form_data.meta and form_data.meta.is_default:
        form_data.meta.is_default = False

    if user.role != "admin" and form_data.access_grants is not None:
        form_data.access_grants = filter_allowed_access_grants(
            request.app.state.config.USER_PERMISSIONS,
            user.id,
            user.role,
            form_data.access_grants,
            "sharing.public_tools",
        )

    if not form_data.id.isidentifier():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only alphanumeric characters and underscores are allowed in the id",
        )

    form_data.id = form_data.id.lower()

    tools = Tools.get_tool_by_id(form_data.id, db=db)
    if tools is None:
        try:
            form_data.content = replace_imports(form_data.content)
            tool_module, frontmatter = load_tool_module_by_id(
                form_data.id, content=form_data.content
            )
            form_data.meta.manifest = frontmatter

            TOOLS = request.app.state.TOOLS
            TOOLS[form_data.id] = tool_module

            specs = get_tool_specs(TOOLS[form_data.id])
            tools = Tools.insert_new_tool(user.id, form_data, specs, db=db)

            tool_cache_dir = CACHE_DIR / "tools" / form_data.id
            tool_cache_dir.mkdir(parents=True, exist_ok=True)

            if tools:
                return tools
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error creating tools"),
                )
        except Exception as e:
            log.exception(f"Failed to load the tool by id {form_data.id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(str(e)),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ID_TAKEN,
        )


############################
# GetToolsById
############################


@router.get("/id/{id}", response_model=Optional[ToolAccessResponse])
async def get_tools_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    tools = Tools.get_tool_by_id(id, db=db)

    if tools:
        user_group_ids = (
            get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        )
        if is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            return ToolAccessResponse(
                **tools.model_dump(),
                write_access=_tool_write_access(
                    user, tools, user_group_ids=user_group_ids, db=db
                ),
                installed=id in _get_installed_tool_ids(user.id, [id], db=db),
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

    return ToolAccessResponse(
        **tools.model_dump(),
        write_access=_tool_write_access(user, tools, db=db),
    )


############################
# InstallToolsById
############################


@router.post("/id/{id}/install", response_model=dict)
async def install_tools_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if id.startswith("server:"):
        found, allowed = await _check_server_tool_access(request, user, id, db=db)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        ResourceInstallations.install_resource(user.id, "tool", id, db=db)
        return {"id": id, "installed": True}

    tool = Tools.get_tool_by_id(id, db=db)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
    if not is_tool_catalog_visible(tool, user, user_group_ids, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    ResourceInstallations.install_resource(user.id, "tool", id, db=db)
    return {"id": id, "installed": True}


@router.delete("/id/{id}/install", response_model=dict)
async def uninstall_tools_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    if id.startswith("server:"):
        found, allowed = await _check_server_tool_access(request, user, id, db=db)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
    ResourceInstallations.uninstall_resource(user.id, "tool", id, db=db)
    return {"id": id, "installed": False}


############################
# UpdateToolsById
############################


@router.post("/id/{id}/update", response_model=Optional[ToolModel])
async def update_tools_by_id(
    request: Request,
    id: str,
    form_data: ToolForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if not tools:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if not _tool_write_access(user, tools, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if user.role != "admin" and form_data.meta and form_data.meta.is_default:
        form_data.meta.is_default = False

    try:
        form_data.content = replace_imports(form_data.content)
        tool_module, frontmatter = load_tool_module_by_id(id, content=form_data.content)
        form_data.meta.manifest = frontmatter

        TOOLS = request.app.state.TOOLS
        TOOLS[id] = tool_module

        specs = get_tool_specs(TOOLS[id])

        updated = {
            **form_data.model_dump(exclude={"id"}),
            "specs": specs,
        }

        log.debug(updated)
        tools = Tools.update_tool_by_id(id, updated, db=db)

        if tools:
            return tools
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating tools"),
            )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# UpdateToolAccessById
############################


class ToolAccessGrantsForm(BaseModel):
    access_grants: list[dict]


@router.post("/id/{id}/access/update", response_model=Optional[ToolModel])
async def update_tool_access_by_id(
    request: Request,
    id: str,
    form_data: ToolAccessGrantsForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if not tools:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if not _tool_write_access(user, tools, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    form_data.access_grants = filter_allowed_access_grants(
        request.app.state.config.USER_PERMISSIONS,
        user.id,
        user.role,
        form_data.access_grants,
        "sharing.public_tools",
    )

    AccessGrants.set_access_grants("tool", id, form_data.access_grants, db=db)

    return Tools.get_tool_by_id(id, db=db)


############################
# DeleteToolsById
############################


@router.delete("/id/{id}/delete", response_model=bool)
async def delete_tools_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if not tools:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if not _tool_write_access(user, tools, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    result = Tools.delete_tool_by_id(id, db=db)
    if result:
        TOOLS = request.app.state.TOOLS
        if id in TOOLS:
            del TOOLS[id]

    return result


############################
# GetToolValves
############################


@router.get("/id/{id}/valves", response_model=Optional[dict])
async def get_tools_valves_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    tools = Tools.get_tool_by_id(id, db=db)
    if tools:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if not is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        try:
            valves = Tools.get_tool_valves_by_id(id, db=db)
            return valves
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(str(e)),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# GetToolValvesSpec
############################


@router.get("/id/{id}/valves/spec", response_model=Optional[dict])
async def get_tools_valves_spec_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if tools:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if not is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        if id in request.app.state.TOOLS:
            tools_module = request.app.state.TOOLS[id]
        else:
            tools_module, _ = load_tool_module_by_id(id)
            request.app.state.TOOLS[id] = tools_module

        if hasattr(tools_module, "Valves"):
            Valves = tools_module.Valves
            schema = Valves.schema()
            # Resolve dynamic options for select dropdowns
            schema = resolve_valves_schema_options(Valves, schema, user)
            return schema
        return None
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# UpdateToolValves
############################


@router.post("/id/{id}/valves/update", response_model=Optional[dict])
async def update_tools_valves_by_id(
    request: Request,
    id: str,
    form_data: dict,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if not tools:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if not _tool_write_access(user, tools, db=db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if id in request.app.state.TOOLS:
        tools_module = request.app.state.TOOLS[id]
    else:
        tools_module, _ = load_tool_module_by_id(id)
        request.app.state.TOOLS[id] = tools_module

    if not hasattr(tools_module, "Valves"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    Valves = tools_module.Valves

    try:
        form_data = {k: v for k, v in form_data.items() if v is not None}
        valves = Valves(**form_data)
        valves_dict = valves.model_dump(exclude_unset=True)
        Tools.update_tool_valves_by_id(id, valves_dict, db=db)
        return valves_dict
    except Exception as e:
        log.exception(f"Failed to update tool valves by id {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# ToolUserValves
############################


@router.get("/id/{id}/valves/user", response_model=Optional[dict])
async def get_tools_user_valves_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    tools = Tools.get_tool_by_id(id, db=db)
    if tools:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if not is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        try:
            user_valves = Tools.get_user_valves_by_id_and_user_id(id, user.id, db=db)
            return user_valves
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(str(e)),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/id/{id}/valves/user/spec", response_model=Optional[dict])
async def get_tools_user_valves_spec_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)
    if tools:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if not is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        if id in request.app.state.TOOLS:
            tools_module = request.app.state.TOOLS[id]
        else:
            tools_module, _ = load_tool_module_by_id(id)
            request.app.state.TOOLS[id] = tools_module

        if hasattr(tools_module, "UserValves"):
            UserValves = tools_module.UserValves
            schema = UserValves.schema()
            # Resolve dynamic options for select dropdowns
            schema = resolve_valves_schema_options(UserValves, schema, user)
            return schema
        return None
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.post("/id/{id}/valves/user/update", response_model=Optional[dict])
async def update_tools_user_valves_by_id(
    request: Request,
    id: str,
    form_data: dict,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    tools = Tools.get_tool_by_id(id, db=db)

    if tools:
        user_group_ids = get_user_group_ids(user.id, db=db) if user.role != "admin" else set()
        if not is_tool_catalog_visible(tools, user, user_group_ids, db=db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        if id in request.app.state.TOOLS:
            tools_module = request.app.state.TOOLS[id]
        else:
            tools_module, _ = load_tool_module_by_id(id)
            request.app.state.TOOLS[id] = tools_module

        if hasattr(tools_module, "UserValves"):
            UserValves = tools_module.UserValves

            try:
                form_data = {k: v for k, v in form_data.items() if v is not None}
                user_valves = UserValves(**form_data)
                user_valves_dict = user_valves.model_dump(exclude_unset=True)
                Tools.update_user_valves_by_id_and_user_id(
                    id, user.id, user_valves_dict, db=db
                )
                return user_valves_dict
            except Exception as e:
                log.exception(f"Failed to update user valves by id {id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(str(e)),
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
