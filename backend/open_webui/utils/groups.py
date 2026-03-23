import logging
from open_webui.models.groups import Groups

log = logging.getLogger(__name__)


def apply_default_group_assignment(
    default_group_id: str,
    user_id: str,
    db=None,
    commit: bool = True,
) -> None:
    """
    Apply default group assignment to a user if default_group_id is provided.

    Args:
        default_group_id: ID of the default group to add the user to
        user_id: ID of the user to add to the default group
    """
    if default_group_id:
        group = Groups.get_group_by_id(default_group_id, db=db)
        if group is None:
            log.warning(
                "Default group %s not found; skipping auto-assignment for user %s",
                default_group_id,
                user_id,
            )
            return
        try:
            assigned_group = Groups.add_users_to_group(
                default_group_id, [user_id], db=db, commit=commit
            )
            if assigned_group is None:
                raise RuntimeError(
                    f"Failed to assign user {user_id} to default group {default_group_id}"
                )
        except Exception as e:
            log.error(
                f"Failed to add user {user_id} to default group {default_group_id}: {e}"
            )
            raise
