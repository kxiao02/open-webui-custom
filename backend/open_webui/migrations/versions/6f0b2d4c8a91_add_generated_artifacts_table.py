"""Add generated artifacts table

Revision ID: 6f0b2d4c8a91
Revises: 5b0a7c9d8e1f
Create Date: 2026-06-22 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.migrations.util import get_existing_tables


revision: str = "6f0b2d4c8a91"
# Intentional for this worktree's delivery train: the local OMS migration
# 5b0a7c9d8e1f is already the migration head, so this revision follows it
# to avoid creating multiple Alembic heads. If generated-artifacts ships
# independently from the OMS work, rebase this down_revision to that branch's
# actual head before merge.
down_revision: Union[str, None] = "5b0a7c9d8e1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if "generated_artifacts" not in existing_tables:
        op.create_table(
            "generated_artifacts",
            sa.Column("artifact_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("storage_provider", sa.String(), nullable=False),
            sa.Column("storage_bucket", sa.Text(), nullable=True),
            sa.Column("s3_key", sa.Text(), nullable=False),
            sa.Column("storage_uri", sa.Text(), nullable=False),
            sa.Column("filename", sa.Text(), nullable=False),
            sa.Column("content_type", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("content_sha256", sa.Text(), nullable=True),
            sa.Column("etag", sa.Text(), nullable=True),
            sa.Column("publication_state", sa.String(), nullable=False),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("owner_chat_id", sa.String(), nullable=True),
            sa.Column("owner_message_id", sa.String(), nullable=True),
            sa.Column("reference_id", sa.String(), nullable=True),
            sa.Column("tool_call_id", sa.String(), nullable=True),
            sa.Column("publication_session_id", sa.String(), nullable=False),
            sa.Column("publication_token_hash", sa.Text(), nullable=False),
            sa.Column("upload_expires_at", sa.BigInteger(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.Column("published_at", sa.BigInteger(), nullable=True),
            sa.Column("deleted_at", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("artifact_id"),
            sa.UniqueConstraint("s3_key", name="uq_generated_artifacts_s3_key"),
            sa.UniqueConstraint(
                "publication_session_id",
                name="uq_generated_artifacts_publication_session_id",
            ),
        )

    op.create_index(
        "ix_generated_artifacts_user_id",
        "generated_artifacts",
        ["user_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_generated_artifacts_owner_chat_id",
        "generated_artifacts",
        ["owner_chat_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_generated_artifacts_owner_message_id",
        "generated_artifacts",
        ["owner_message_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_generated_artifacts_reference_id",
        "generated_artifacts",
        ["reference_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_generated_artifacts_tool_call_id",
        "generated_artifacts",
        ["tool_call_id"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_generated_artifacts_owner_message",
        "generated_artifacts",
        ["owner_chat_id", "owner_message_id"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_generated_artifacts_user_state",
        "generated_artifacts",
        ["user_id", "publication_state"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_generated_artifacts_user_state",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "idx_generated_artifacts_owner_message",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_tool_call_id",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_reference_id",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_owner_message_id",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_owner_chat_id",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_index(
        "ix_generated_artifacts_user_id",
        table_name="generated_artifacts",
        if_exists=True,
    )
    op.drop_table("generated_artifacts", if_exists=True)
