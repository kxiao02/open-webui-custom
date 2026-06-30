"""Add wxwork OMS relation model table

Revision ID: 5b0a7c9d8e1f
Revises: e8f9a1b2c3d4
Create Date: 2026-05-21 16:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.migrations.util import get_existing_tables


revision: str = "5b0a7c9d8e1f"
down_revision: Union[str, None] = "e8f9a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if "wxwork_oms_identity_model" not in existing_tables:
        op.create_table(
            "wxwork_oms_identity_model",
            sa.Column("fd_ekp_id", sa.Text(), nullable=False, primary_key=True),
            sa.Column("fd_id", sa.Text(), nullable=False),
            sa.Column("fd_name", sa.Text(), nullable=False),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("fd_id", name="uq_wxwork_oms_identity_model_fd_id"),
        )

    if "wxwork_oms_relation_model" not in existing_tables:
        op.create_table(
            "wxwork_oms_relation_model",
            sa.Column("fd_app_pk_id", sa.Text(), nullable=False, primary_key=True),
            sa.Column("fd_ekp_id", sa.Text(), nullable=False),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["fd_ekp_id"],
                ["wxwork_oms_identity_model.fd_ekp_id"],
                name="fk_wxwork_oms_relation_model_fd_ekp_id",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "fd_ekp_id", name="uq_wxwork_oms_relation_model_fd_ekp_id"
            ),
        )

    op.create_index(
        "idx_wxwork_oms_relation_model_fd_ekp_id",
        "wxwork_oms_relation_model",
        ["fd_ekp_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_wxwork_oms_relation_model_fd_ekp_id",
        table_name="wxwork_oms_relation_model",
        if_exists=True,
    )
    op.drop_table("wxwork_oms_relation_model", if_exists=True)
    op.drop_table("wxwork_oms_identity_model", if_exists=True)
