"""Add resource_installation table

Revision ID: d4e5f6a7b8c9
Revises: a1b2c3d4e5f6
Create Date: 2026-03-21 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.migrations.util import get_existing_tables

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if "resource_installation" not in existing_tables:
        op.create_table(
            "resource_installation",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("resource_type", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "resource_type",
                "resource_id",
                name="uq_resource_installation_user_resource",
            ),
        )
        op.create_index(
            "idx_resource_installation_user_type",
            "resource_installation",
            ["user_id", "resource_type"],
        )
        op.create_index(
            "idx_resource_installation_resource",
            "resource_installation",
            ["resource_type", "resource_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "idx_resource_installation_resource", table_name="resource_installation"
    )
    op.drop_index(
        "idx_resource_installation_user_type", table_name="resource_installation"
    )
    op.drop_table("resource_installation")
