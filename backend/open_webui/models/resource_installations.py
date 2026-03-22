import logging
import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, UniqueConstraint
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db_context

log = logging.getLogger(__name__)


class ResourceInstallation(Base):
    __tablename__ = "resource_installation"

    id = Column(String, primary_key=True, unique=True)
    user_id = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)
    resource_id = Column(String, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "resource_type",
            "resource_id",
            name="uq_resource_installation_user_resource",
        ),
    )


class ResourceInstallationModel(BaseModel):
    id: str
    user_id: str
    resource_type: Literal["tool", "skill"]
    resource_id: str
    updated_at: int
    created_at: int

    model_config = ConfigDict(from_attributes=True)


class ResourceInstallationsTable:
    def get_installed_resource_ids(
        self,
        user_id: str,
        resource_type: Literal["tool", "skill"],
        resource_ids: Optional[list[str] | set[str] | tuple[str, ...]] = None,
        db: Optional[Session] = None,
    ) -> set[str]:
        with get_db_context(db) as db:
            query = db.query(ResourceInstallation.resource_id).filter_by(
                user_id=user_id, resource_type=resource_type
            )

            if resource_ids:
                query = query.filter(
                    ResourceInstallation.resource_id.in_(list(resource_ids))
                )

            return {row[0] for row in query.all()}

    def install_resource(
        self,
        user_id: str,
        resource_type: Literal["tool", "skill"],
        resource_id: str,
        db: Optional[Session] = None,
    ) -> ResourceInstallationModel:
        with get_db_context(db) as db:
            existing = (
                db.query(ResourceInstallation)
                .filter_by(
                    user_id=user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
                .first()
            )

            now = int(time.time())
            if existing:
                existing.updated_at = now
                db.commit()
                db.refresh(existing)
                return ResourceInstallationModel.model_validate(existing)

            installation = ResourceInstallation(
                id=str(uuid.uuid4()),
                user_id=user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                updated_at=now,
                created_at=now,
            )
            db.add(installation)
            db.commit()
            db.refresh(installation)
            return ResourceInstallationModel.model_validate(installation)

    def uninstall_resource(
        self,
        user_id: str,
        resource_type: Literal["tool", "skill"],
        resource_id: str,
        db: Optional[Session] = None,
    ) -> bool:
        with get_db_context(db) as db:
            deleted = (
                db.query(ResourceInstallation)
                .filter_by(
                    user_id=user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
                .delete()
            )
            db.commit()
            return deleted > 0


ResourceInstallations = ResourceInstallationsTable()
