import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

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


def _build_skill_form(skill_id: str, raw_text: str) -> SkillForm:
    frontmatter, body = _split_frontmatter(raw_text)
    metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    name = str(frontmatter.get("name") or skill_id)
    description = frontmatter.get("description") or ""
    if isinstance(description, (list, dict)):
        description = str(description)

    category = metadata.get("category") if isinstance(metadata, dict) else None
    if not isinstance(category, str) or not category.strip():
        category = "document"

    phase_note = PHASE_NOTES.get(skill_id, "")
    content = body.strip()
    if phase_note:
        content = f"{phase_note}\n\n{content}"

    meta = SkillMeta(
        tags=["minimax", "document"],
        published=True,
        category=category,
        visibility="public",
        dependencies=[],
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


def sync_minimax_document_skills(
    *,
    user_id: str,
    root_path: Optional[str] = None,
    db=None,
    dry_run: bool = False,
) -> dict:
    root = _resolve_minimax_skills_root(root_path)
    if not root.exists():
        raise FileNotFoundError(f"MiniMax skills directory not found: {root}")

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for skill_id in MINIMAX_DOCUMENT_SKILL_IDS:
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
                    updated.append(skill_id)
                else:
                    created.append(skill_id)
                continue

            if existing:
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
        "errors": errors,
    }
