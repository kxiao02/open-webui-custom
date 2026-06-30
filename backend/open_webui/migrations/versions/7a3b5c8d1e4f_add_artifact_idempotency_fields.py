"""Add artifact idempotency fields

Revision ID: 7a3b5c8d1e4f
Revises: 6f0b2d4c8a91
Create Date: 2026-06-28 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a3b5c8d1e4f"
down_revision: Union[str, None] = "6f0b2d4c8a91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generated_artifacts",
        sa.Column("scope_token_jti", sa.String(), nullable=True),
    )
    op.add_column(
        "generated_artifacts",
        sa.Column("reservation_idempotency_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "generated_artifacts",
        sa.Column("reservation_fingerprint", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_generated_artifacts_scope_token_jti",
        "generated_artifacts",
        ["scope_token_jti"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_generated_artifacts_idempotency",
        "generated_artifacts",
        ["scope_token_jti", "reservation_idempotency_key"],
        unique=True,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_generated_artifacts_idempotency",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_scope_token_jti",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_column("generated_artifacts", "reservation_fingerprint")
    op.drop_column("generated_artifacts", "reservation_idempotency_key")
    op.drop_column("generated_artifacts", "scope_token_jti")
