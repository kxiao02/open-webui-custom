import os
from typing import Optional


_DEFAULT_ALWAYS_ALLOWED_MODEL_IDS = {
    "deepagent",
    "deepagent-thinkmodels",
}

_ENV_ALWAYS_ALLOWED_MODEL_IDS = {
    model_id.strip()
    for model_id in os.environ.get("ALWAYS_ALLOWED_MODEL_IDS", "").split(",")
    if model_id.strip()
}

ALWAYS_ALLOWED_MODEL_IDS = (
    _DEFAULT_ALWAYS_ALLOWED_MODEL_IDS | _ENV_ALWAYS_ALLOWED_MODEL_IDS
)


def _candidate_model_ids(model_id: Optional[str]) -> set[str]:
    if not model_id:
        return set()

    normalized = model_id.strip()
    if not normalized:
        return set()

    candidates = {normalized}

    model_without_tag = normalized.split(":", 1)[0]
    candidates.add(model_without_tag)

    if "." in model_without_tag:
        candidates.add(model_without_tag.rsplit(".", 1)[-1])

    return {candidate for candidate in candidates if candidate}


def is_model_always_allowed(model_id: Optional[str]) -> bool:
    return len(_candidate_model_ids(model_id) & ALWAYS_ALLOWED_MODEL_IDS) > 0
