import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from open_webui.models.skills import SkillForm, SkillMeta, Skills

log = logging.getLogger(__name__)

MINIMAX_DOCUMENT_SKILL_IDS = (
    "minimax-pdf",
    "minimax-xlsx",
    "pptx-generator",
    "minimax-docx",
)

PHASE_NOTES = {
    "minimax-pdf": "【中电慧语文档能力】当前版本提供 PDF 工具化工作流，优先调用 `pdf_create_document`、`pdf_inspect_form`、`pdf_fill_form_tool`、`pdf_reformat_document`。如果运行时依赖缺失，必须明确说明限制，不要假设外部脚本天然可用。",
    "minimax-xlsx": "【中电慧语文档能力】当前版本提供 XLSX 工具化工作流，优先调用 `xlsx_read_workbook`、`xlsx_validate_workbook`、`xlsx_add_column_tool`、`xlsx_insert_row_tool`。如果运行时依赖缺失，必须明确说明限制，不要假设外部脚本天然可用。",
    "pptx-generator": "【中电慧语文档能力】当前版本提供基线版 PPTX 工具化工作流，优先调用 `pptx_export_presentation` 生成 Markdown/大纲驱动的最佳努力导出结果。它不是 MiniMax 高保真模板/母版编辑能力；如果用户追求严格模板复刻或精细版式，请先明确当前限制。",
    "minimax-docx": "【中电慧语文档能力】当前版本提供最小可用的 Markdown->DOCX 生成路径：优先使用 MiniMax DOCX 运行时，模板参考场景回退为近似样式导出。复杂模板/高保真 OpenXML 编辑尚未上线，如需严格模板或精细版式请先说明限制。",
}


def _resolve_minimax_skills_root(root_path: Optional[str] = None) -> Path:
    if root_path:
        return Path(root_path)

    env_path = os.getenv("MINIMAX_SKILLS_DIR") or os.getenv("DOCUMENT_SKILLS_DIR")
    if env_path:
        return Path(env_path)

    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "deepagent-project" / "minimax-skills" / "skills"


def _discover_skill_ids(root: Path) -> list[str]:
    discovered: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").exists():
            discovered.append(entry.name)
    return discovered


def _normalize_skill_ids(skill_ids: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for skill_id in skill_ids or []:
        candidate = str(skill_id or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _split_frontmatter(raw: str) -> tuple[Dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break

    if end_idx is None:
        return {}, raw

    frontmatter_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    try:
        parsed = yaml.safe_load(frontmatter_text) or {}
    except Exception as exc:
        log.warning("Failed to parse skill frontmatter: %s", exc)
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    return parsed, body


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _normalize_visibility(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {"public", "restricted", "hidden"}:
        return candidate
    return "public"


def _build_skill_form(skill_id: str, raw_text: str) -> SkillForm:
    frontmatter, body = _split_frontmatter(raw_text)
    metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    name = str(frontmatter.get("name") or skill_id)
    description = frontmatter.get("description") or ""
    if isinstance(description, (list, dict)):
        description = str(description)

    category = frontmatter.get("category") or metadata.get("category")
    if not isinstance(category, str) or not category.strip():
        category = "document" if skill_id in PHASE_NOTES else "general"
    category = category.strip()

    tags = _coerce_str_list(frontmatter.get("tags")) or _coerce_str_list(
        metadata.get("tags")
    )
    if not tags:
        tags = ["minimax"]
        if category not in tags:
            tags.append(category)

    dependencies = _coerce_str_list(frontmatter.get("dependencies")) or _coerce_str_list(
        metadata.get("dependencies")
    )
    visibility = _normalize_visibility(
        frontmatter.get("visibility") or metadata.get("visibility")
    )
    published = bool(frontmatter.get("published", metadata.get("published", True)))

    phase_note = PHASE_NOTES.get(skill_id, "")
    content = body.strip()
    if phase_note:
        content = f"{phase_note}\n\n{content}"

    meta = SkillMeta(
        tags=tags,
        published=published,
        category=category,
        visibility=visibility,
        dependencies=dependencies,
        is_default=False,
    )

    return SkillForm(
        id=skill_id,
        name=name,
        description=description,
        content=content,
        meta=meta,
        is_active=True,
    )


def sync_minimax_skills(
    *,
    user_id: str,
    root_path: Optional[str] = None,
    skill_ids: Iterable[str] | None = None,
    db=None,
    dry_run: bool = False,
    overwrite_existing: bool = True,
) -> dict:
    root = _resolve_minimax_skills_root(root_path)
    if not root.exists():
        raise FileNotFoundError(f"MiniMax skills directory not found: {root}")

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    skipped_existing: list[str] = []
    errors: list[dict] = []
    resolved_skill_ids = _normalize_skill_ids(skill_ids) or _discover_skill_ids(root)

    for skill_id in resolved_skill_ids:
        skill_dir = root / skill_id
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            skipped.append(skill_id)
            continue

        try:
            raw_text = skill_file.read_text(encoding="utf-8")
            form = _build_skill_form(skill_id, raw_text)
            existing = Skills.get_skill_by_id(skill_id, db=db)

            if dry_run:
                if existing:
                    if overwrite_existing:
                        updated.append(skill_id)
                    else:
                        skipped_existing.append(skill_id)
                else:
                    created.append(skill_id)
                continue

            if existing:
                if not overwrite_existing:
                    skipped_existing.append(skill_id)
                    continue
                updated_payload = {
                    "name": form.name,
                    "description": form.description,
                    "content": form.content,
                    "meta": form.meta.model_dump(),
                    "is_active": True,
                }
                updated_skill = Skills.update_skill_by_id(
                    skill_id, updated_payload, db=db
                )
                if updated_skill:
                    updated.append(skill_id)
                else:
                    errors.append(
                        {"id": skill_id, "error": "update_failed"}
                    )
            else:
                created_skill = Skills.insert_new_skill(
                    user_id, form, db=db
                )
                if created_skill:
                    created.append(skill_id)
                else:
                    errors.append(
                        {"id": skill_id, "error": "create_failed"}
                    )
        except Exception as exc:
            errors.append({"id": skill_id, "error": str(exc)})

    return {
        "root": str(root),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }


def _resolve_skill_seed_owner_id(explicit_user_id: str | None = None) -> str:
    candidate = str(explicit_user_id or "").strip()
    if candidate:
        return candidate

    from open_webui.models.users import Users
    from open_webui.utils.auth import create_admin_user

    admin_email = str(os.getenv("WEBUI_ADMIN_EMAIL") or "").strip()
    admin_password = str(os.getenv("WEBUI_ADMIN_PASSWORD") or "").strip()
    admin_name = str(os.getenv("WEBUI_ADMIN_NAME") or "Admin").strip() or "Admin"

    if admin_email and admin_password:
        admin_user = create_admin_user(admin_email, admin_password, admin_name)
        if admin_user and getattr(admin_user, "id", None):
            return str(admin_user.id)

    first_admin = Users.get_first_admin_user()
    if first_admin and getattr(first_admin, "id", None):
        return str(first_admin.id)

    first_user = Users.get_first_user()
    if first_user and getattr(first_user, "id", None):
        return str(first_user.id)

    return "system:minimax-seed"


def seed_minimax_skills_once(
    *,
    root_path: Optional[str] = None,
    skill_ids: Iterable[str] | None = MINIMAX_DOCUMENT_SKILL_IDS,
    user_id: Optional[str] = None,
    db=None,
    dry_run: bool = False,
) -> dict:
    owner_id = _resolve_skill_seed_owner_id(user_id)
    result = sync_minimax_skills(
        user_id=owner_id,
        root_path=root_path,
        skill_ids=skill_ids,
        db=db,
        dry_run=dry_run,
        overwrite_existing=False,
    )
    result["owner_id"] = owner_id
    return result


def sync_minimax_document_skills(
    *,
    user_id: str,
    root_path: Optional[str] = None,
    db=None,
    dry_run: bool = False,
) -> dict:
    return sync_minimax_skills(
        user_id=user_id,
        root_path=root_path,
        db=db,
        dry_run=dry_run,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed bundled MiniMax skills into the Open WebUI skill catalog."
    )
    parser.add_argument("--root-path", dest="root_path")
    parser.add_argument(
        "--all-discovered",
        action="store_true",
        help="Import every discovered skill directory instead of the default document skill allowlist.",
    )
    parser.add_argument(
        "--skill-id",
        dest="skill_ids",
        action="append",
        default=None,
        help="Import only the specified skill id. Can be repeated.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Update existing DB skills instead of skipping them.",
    )
    parser.add_argument("--user-id", dest="user_id")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    selected_skill_ids = None if args.all_discovered else (
        _normalize_skill_ids(args.skill_ids) or list(MINIMAX_DOCUMENT_SKILL_IDS)
    )

    if args.overwrite_existing:
        owner_id = _resolve_skill_seed_owner_id(args.user_id)
        result = sync_minimax_skills(
            user_id=owner_id,
            root_path=args.root_path,
            skill_ids=selected_skill_ids,
            dry_run=args.dry_run,
            overwrite_existing=True,
        )
        result["owner_id"] = owner_id
    else:
        result = seed_minimax_skills_once(
            root_path=args.root_path,
            skill_ids=selected_skill_ids,
            user_id=args.user_id,
            dry_run=args.dry_run,
        )

    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
