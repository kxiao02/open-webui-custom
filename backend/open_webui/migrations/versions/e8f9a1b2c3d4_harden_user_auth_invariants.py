"""Harden user/auth invariants

Revision ID: e8f9a1b2c3d4
Revises: b2c3d4e5f6a7, c0fbf31ca0db, d4e5f6a7b8c9
Create Date: 2026-03-23 16:20:00.000000

"""

from __future__ import annotations

import json
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e8f9a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = (
    "b2c3d4e5f6a7",
    "c0fbf31ca0db",
    "d4e5f6a7b8c9",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRIMARY_ADMIN_INFO_KEY = "primary_admin"
PRIMARY_ADMIN_SET_AT_KEY = "primary_admin_set_at"


user_table = sa.table(
    "user",
    sa.column("id", sa.String()),
    sa.column("email", sa.String()),
    sa.column("role", sa.String()),
    sa.column("info", sa.JSON()),
    sa.column("created_at", sa.BigInteger()),
)

auth_table = sa.table(
    "auth",
    sa.column("id", sa.String()),
    sa.column("email", sa.String()),
    sa.column("password", sa.Text()),
    sa.column("active", sa.Boolean()),
)


def _load_jsonish(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except Exception:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def _normalize_emails(conn) -> None:
    conn.execute(
        sa.update(user_table)
        .where(user_table.c.email.is_not(None))
        .values(email=sa.func.lower(user_table.c.email))
    )
    conn.execute(
        sa.update(auth_table)
        .where(auth_table.c.email.is_not(None))
        .values(email=sa.func.lower(auth_table.c.email))
    )


def _delete_orphan_auth_rows(conn) -> None:
    conn.execute(
        sa.text(
            'DELETE FROM auth WHERE NOT EXISTS (SELECT 1 FROM "user" WHERE "user".id = auth.id)'
        )
    )


def _assert_no_duplicate_user_emails(conn) -> None:
    duplicates = conn.execute(
        sa.text(
            'SELECT lower(email) AS email FROM "user" GROUP BY lower(email) HAVING COUNT(*) > 1'
        )
    ).fetchall()
    if duplicates:
        duplicate_values = ", ".join(row[0] for row in duplicates if row[0])
        raise RuntimeError(
            "Cannot enforce uq_user_email because duplicate user emails exist: "
            f"{duplicate_values}"
        )


def _backfill_primary_admin(conn) -> None:
    rows = conn.execute(
        sa.select(
            user_table.c.id,
            user_table.c.info,
            user_table.c.created_at,
        )
        .where(user_table.c.role == "admin")
        .order_by(user_table.c.created_at.asc(), user_table.c.id.asc())
    ).fetchall()

    if not rows:
        return

    primary_id = None
    normalized_info = {}

    for row in rows:
        info = _load_jsonish(row.info) or {}
        if primary_id is None and info.get(PRIMARY_ADMIN_INFO_KEY) is True:
            primary_id = row.id
        info.pop(PRIMARY_ADMIN_INFO_KEY, None)
        info.pop(PRIMARY_ADMIN_SET_AT_KEY, None)
        normalized_info[row.id] = info

    if primary_id is None:
        primary_id = rows[0].id

    for row in rows:
        info = normalized_info[row.id]
        if row.id == primary_id:
            info[PRIMARY_ADMIN_INFO_KEY] = True
            info[PRIMARY_ADMIN_SET_AT_KEY] = int(time.time())

        conn.execute(
            sa.update(user_table)
            .where(user_table.c.id == row.id)
            .values(info=info or None)
        )


def upgrade() -> None:
    conn = op.get_bind()

    _normalize_emails(conn)
    _delete_orphan_auth_rows(conn)
    _backfill_primary_admin(conn)
    _assert_no_duplicate_user_emails(conn)

    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column("email", existing_type=sa.String(), nullable=False)
        batch_op.create_unique_constraint("uq_user_email", ["email"])

    with op.batch_alter_table("auth") as batch_op:
        batch_op.alter_column("email", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("password", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("active", existing_type=sa.Boolean(), nullable=False)
        batch_op.create_foreign_key(
            "fk_auth_id_user",
            "user",
            ["id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("auth") as batch_op:
        batch_op.drop_constraint("fk_auth_id_user", type_="foreignkey")

    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint("uq_user_email", type_="unique")
