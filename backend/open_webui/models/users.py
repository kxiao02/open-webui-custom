import time
from typing import Optional

from sqlalchemy.orm import Session, defer
from open_webui.internal.db import Base, JSONField, get_db, get_db_context


from open_webui.env import DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL

from open_webui.models.chats import Chats
from open_webui.models.groups import Group, GroupMember
from open_webui.models.channels import ChannelMember

from open_webui.utils.misc import throttle
from open_webui.utils.validate import validate_profile_image_url


from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    JSON,
    Column,
    String,
    Boolean,
    Text,
    Date,
    UniqueConstraint,
    exists,
    select,
    cast,
)
from sqlalchemy import or_, case, func
from sqlalchemy.dialects.postgresql import JSONB

import datetime

####################
# User DB Schema
####################

PRIMARY_ADMIN_INFO_KEY = "primary_admin"
PRIMARY_ADMIN_SET_AT_KEY = "primary_admin_set_at"


class UserSettings(BaseModel):
    ui: Optional[dict] = {}
    model_config = ConfigDict(extra="allow")
    pass


class User(Base):
    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    id = Column(String, primary_key=True, unique=True)
    email = Column(String, nullable=False)
    username = Column(String(50), nullable=True)
    role = Column(String, nullable=False)

    name = Column(String, nullable=False)

    profile_image_url = Column(Text, nullable=False)
    profile_banner_image_url = Column(Text, nullable=True)

    bio = Column(Text, nullable=True)
    gender = Column(Text, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    timezone = Column(String, nullable=True)

    presence_state = Column(String, nullable=True)
    status_emoji = Column(String, nullable=True)
    status_message = Column(Text, nullable=True)
    status_expires_at = Column(BigInteger, nullable=True)

    info = Column(JSON, nullable=True)
    settings = Column(JSON, nullable=True)

    oauth = Column(JSON, nullable=True)
    scim = Column(JSON, nullable=True)

    last_active_at = Column(BigInteger)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class UserModel(BaseModel):
    id: str

    email: str
    username: Optional[str] = None
    role: str = "pending"

    name: str

    profile_image_url: Optional[str] = None
    profile_banner_image_url: Optional[str] = None

    bio: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime.date] = None
    timezone: Optional[str] = None

    presence_state: Optional[str] = None
    status_emoji: Optional[str] = None
    status_message: Optional[str] = None
    status_expires_at: Optional[int] = None

    info: Optional[dict] = None
    settings: Optional[UserSettings] = None

    oauth: Optional[dict] = None
    scim: Optional[dict] = None

    last_active_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def set_profile_image_url(self):
        if not self.profile_image_url:
            self.profile_image_url = f"/api/v1/users/{self.id}/profile/image"
        return self


class UserStatusModel(UserModel):
    is_active: bool = False

    model_config = ConfigDict(from_attributes=True)


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text, nullable=False)
    key = Column(Text, unique=True, nullable=False)
    data = Column(JSON, nullable=True)
    expires_at = Column(BigInteger, nullable=True)
    last_used_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class ApiKeyModel(BaseModel):
    id: str
    user_id: str
    key: str
    data: Optional[dict] = None
    expires_at: Optional[int] = None
    last_used_at: Optional[int] = None
    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class UpdateProfileForm(BaseModel):
    profile_image_url: str
    name: str
    bio: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime.date] = None

    @field_validator("profile_image_url")
    @classmethod
    def check_profile_image_url(cls, v: str) -> str:
        return validate_profile_image_url(v)


class UserGroupIdsModel(UserModel):
    group_ids: list[str] = []


class UserModelResponse(UserModel):
    model_config = ConfigDict(extra="allow")


class UserListResponse(BaseModel):
    users: list[UserModelResponse]
    total: int


class UserGroupIdsListResponse(BaseModel):
    users: list[UserGroupIdsModel]
    total: int


class UserStatus(BaseModel):
    status_emoji: Optional[str] = None
    status_message: Optional[str] = None
    status_expires_at: Optional[int] = None


class UserInfoResponse(UserStatus):
    id: str
    name: str
    email: str
    role: str
    bio: Optional[str] = None
    groups: Optional[list] = []
    is_active: bool = False


class UserIdNameResponse(BaseModel):
    id: str
    name: str


class UserIdNameStatusResponse(UserStatus):
    id: str
    name: str
    is_active: Optional[bool] = None


class UserInfoListResponse(BaseModel):
    users: list[UserInfoResponse]
    total: int


class UserIdNameListResponse(BaseModel):
    users: list[UserIdNameResponse]
    total: int


class UserNameResponse(BaseModel):
    id: str
    name: str
    role: str


class UserResponse(UserNameResponse):
    email: str


class UserProfileImageResponse(UserNameResponse):
    email: str
    profile_image_url: str


class UserRoleUpdateForm(BaseModel):
    id: str
    role: str


class UserUpdateForm(BaseModel):
    role: str
    name: str
    email: str
    profile_image_url: str
    password: Optional[str] = None

    @field_validator("profile_image_url")
    @classmethod
    def check_profile_image_url(cls, v: str) -> str:
        return validate_profile_image_url(v)


class UsersTable:
    def _normalize_user_info(self, info: Optional[dict]) -> dict:
        return info if isinstance(info, dict) else {}

    def _set_primary_admin_flag(self, user: User, value: bool) -> bool:
        info = self._normalize_user_info(user.info)
        changed = False

        if value:
            if info.get(PRIMARY_ADMIN_INFO_KEY) is not True:
                info[PRIMARY_ADMIN_INFO_KEY] = True
                info.setdefault(PRIMARY_ADMIN_SET_AT_KEY, int(time.time()))
                changed = True
        else:
            if PRIMARY_ADMIN_INFO_KEY in info:
                info.pop(PRIMARY_ADMIN_INFO_KEY, None)
                info.pop(PRIMARY_ADMIN_SET_AT_KEY, None)
                changed = True

        if changed:
            user.info = info if info else None

        return changed

    def _find_primary_admin_row(self, db: Session) -> Optional[User]:
        admins = (
            db.query(User).filter(User.role == "admin").order_by(User.created_at).all()
        )
        for admin in admins:
            info = self._normalize_user_info(admin.info)
            if info.get(PRIMARY_ADMIN_INFO_KEY) is True:
                return admin
        return None

    def _clear_primary_admin_flags(
        self, db: Session, exclude_user_id: Optional[str] = None
    ) -> bool:
        changed = False
        admins = db.query(User).filter(User.role == "admin").all()
        for admin in admins:
            if exclude_user_id and admin.id == exclude_user_id:
                continue
            info = self._normalize_user_info(admin.info)
            if info.get(PRIMARY_ADMIN_INFO_KEY) is True:
                if self._set_primary_admin_flag(admin, False):
                    changed = True
        return changed

    def ensure_primary_admin(
        self,
        user_id: Optional[str] = None,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                primary = self._find_primary_admin_row(db)
                changed = False

                if primary:
                    changed = self._clear_primary_admin_flags(
                        db, exclude_user_id=primary.id
                    )
                else:
                    candidate = None
                    if user_id:
                        candidate = db.query(User).filter_by(id=user_id).first()
                        if candidate and candidate.role != "admin":
                            candidate = None
                    if not candidate:
                        candidate = (
                            db.query(User)
                            .filter(User.role == "admin")
                            .order_by(User.created_at)
                            .first()
                        )
                    if candidate:
                        if self._set_primary_admin_flag(candidate, True):
                            changed = True
                        primary = candidate

                if changed:
                    if commit:
                        db.commit()
                        if primary:
                            db.refresh(primary)
                    else:
                        db.flush()

                return UserModel.model_validate(primary) if primary else None
        except Exception:
            return None

    def get_primary_admin_user(
        self, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                primary = self._find_primary_admin_row(db)
                return UserModel.model_validate(primary) if primary else None
        except Exception:
            return None

    def insert_new_user(
        self,
        id: str,
        name: str,
        email: str,
        profile_image_url: str = "/user.png",
        role: str = "pending",
        username: Optional[str] = None,
        oauth: Optional[dict] = None,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        with get_db_context(db) as db:
            user = UserModel(
                **{
                    "id": id,
                    "email": email,
                    "name": name,
                    "role": role,
                    "profile_image_url": profile_image_url,
                    "last_active_at": int(time.time()),
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                    "username": username,
                    "oauth": oauth,
                }
            )
            result = User(**user.model_dump())
            db.add(result)
            if commit:
                db.commit()
                db.refresh(result)
            else:
                db.flush()
            if result:
                return user
            else:
                return None

    def get_user_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                return UserModel.model_validate(user)
        except Exception:
            return None

    def get_user_by_api_key(
        self, api_key: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = (
                    db.query(User)
                    .join(ApiKey, User.id == ApiKey.user_id)
                    .filter(ApiKey.key == api_key)
                    .first()
                )
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    def get_user_by_email(
        self, email: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = (
                    db.query(User)
                    .filter(func.lower(User.email) == email.lower())
                    .order_by(User.created_at.asc(), User.id.asc())
                    .first()
                )
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    def get_user_by_oauth_sub(
        self, provider: str, sub: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        return self.get_user_by_oauth_provider_field(
            provider, "sub", sub, db=db
        )

    def get_user_by_oauth_provider_field(
        self, provider: str, field: str, value: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:  # type: Session
                dialect_name = db.bind.dialect.name

                query = db.query(User)
                if dialect_name == "sqlite":
                    query = query.filter(
                        User.oauth.contains({provider: {field: value}})
                    )
                elif dialect_name == "postgresql":
                    query = query.filter(
                        User.oauth[provider].cast(JSONB)[field].astext == value
                    )

                user = query.first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    def get_user_by_scim_external_id(
        self, provider: str, external_id: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:  # type: Session
                dialect_name = db.bind.dialect.name

                query = db.query(User)
                if dialect_name == "sqlite":
                    query = query.filter(
                        User.scim.contains({provider: {"external_id": external_id}})
                    )
                elif dialect_name == "postgresql":
                    query = query.filter(
                        User.scim[provider].cast(JSONB)["external_id"].astext
                        == external_id
                    )

                user = query.first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    def get_users(
        self,
        filter: Optional[dict] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> dict:
        with get_db_context(db) as db:
            # Join GroupMember so we can order by group_id when requested
            query = db.query(User).options(defer(User.profile_image_url))

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(
                        or_(
                            User.name.ilike(f"%{query_key}%"),
                            User.email.ilike(f"%{query_key}%"),
                        )
                    )

                channel_id = filter.get("channel_id")
                if channel_id:
                    query = query.filter(
                        exists(
                            select(ChannelMember.id).where(
                                ChannelMember.user_id == User.id,
                                ChannelMember.channel_id == channel_id,
                            )
                        )
                    )

                user_ids = filter.get("user_ids")
                group_ids = filter.get("group_ids")

                if isinstance(user_ids, list) and isinstance(group_ids, list):
                    # If both are empty lists, return no users
                    if not user_ids and not group_ids:
                        return {"users": [], "total": 0}

                if user_ids:
                    query = query.filter(User.id.in_(user_ids))

                if group_ids:
                    query = query.filter(
                        exists(
                            select(GroupMember.id).where(
                                GroupMember.user_id == User.id,
                                GroupMember.group_id.in_(group_ids),
                            )
                        )
                    )

                roles = filter.get("roles")
                if roles:
                    include_roles = [role for role in roles if not role.startswith("!")]
                    exclude_roles = [role[1:] for role in roles if role.startswith("!")]

                    if include_roles:
                        query = query.filter(User.role.in_(include_roles))
                    if exclude_roles:
                        query = query.filter(~User.role.in_(exclude_roles))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and order_by.startswith("group_id:"):
                    group_id = order_by.split(":", 1)[1]

                    # Subquery that checks if the user belongs to the group
                    membership_exists = exists(
                        select(GroupMember.id).where(
                            GroupMember.user_id == User.id,
                            GroupMember.group_id == group_id,
                        )
                    )

                    # CASE: user in group → 1, user not in group → 0
                    group_sort = case((membership_exists, 1), else_=0)

                    if direction == "asc":
                        query = query.order_by(group_sort.asc(), User.name.asc())
                    else:
                        query = query.order_by(group_sort.desc(), User.name.asc())

                elif order_by == "name":
                    if direction == "asc":
                        query = query.order_by(User.name.asc())
                    else:
                        query = query.order_by(User.name.desc())

                elif order_by == "email":
                    if direction == "asc":
                        query = query.order_by(User.email.asc())
                    else:
                        query = query.order_by(User.email.desc())

                elif order_by == "created_at":
                    if direction == "asc":
                        query = query.order_by(User.created_at.asc())
                    else:
                        query = query.order_by(User.created_at.desc())

                elif order_by == "last_active_at":
                    if direction == "asc":
                        query = query.order_by(User.last_active_at.asc())
                    else:
                        query = query.order_by(User.last_active_at.desc())

                elif order_by == "updated_at":
                    if direction == "asc":
                        query = query.order_by(User.updated_at.asc())
                    else:
                        query = query.order_by(User.updated_at.desc())
                elif order_by == "role":
                    if direction == "asc":
                        query = query.order_by(User.role.asc())
                    else:
                        query = query.order_by(User.role.desc())

            else:
                query = query.order_by(User.created_at.desc())

            # Count BEFORE pagination
            total = query.count()

            # correct pagination logic
            if skip is not None:
                query = query.offset(skip)
            if limit is not None:
                query = query.limit(limit)

            users = query.all()
            return {
                "users": [UserModel.model_validate(user) for user in users],
                "total": total,
            }

    def get_users_by_group_id(
        self, group_id: str, db: Optional[Session] = None
    ) -> list[UserModel]:
        with get_db_context(db) as db:
            users = (
                db.query(User)
                .options(defer(User.profile_image_url))
                .join(GroupMember, User.id == GroupMember.user_id)
                .filter(GroupMember.group_id == group_id)
                .all()
            )
            return [UserModel.model_validate(user) for user in users]

    def get_users_by_user_ids(
        self, user_ids: list[str], db: Optional[Session] = None
    ) -> list[UserStatusModel]:
        with get_db_context(db) as db:
            users = (
                db.query(User)
                .options(defer(User.profile_image_url))
                .filter(User.id.in_(user_ids))
                .all()
            )
            return [UserModel.model_validate(user) for user in users]

    def get_num_users(self, db: Optional[Session] = None) -> Optional[int]:
        with get_db_context(db) as db:
            return db.query(User).count()

    def has_users(self, db: Optional[Session] = None) -> bool:
        with get_db_context(db) as db:
            return db.query(db.query(User).exists()).scalar()

    def get_first_user(self, db: Optional[Session] = None) -> UserModel:
        try:
            with get_db_context(db) as db:
                user = db.query(User).order_by(User.created_at).first()
                return UserModel.model_validate(user)
        except Exception:
            return None

    def get_first_admin_user(self, db: Optional[Session] = None) -> UserModel:
        try:
            with get_db_context(db) as db:
                user = (
                    db.query(User)
                    .filter(User.role == "admin")
                    .order_by(User.created_at)
                    .first()
                )
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    def get_user_webhook_url_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[str]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()

                if user.settings is None:
                    return None
                else:
                    return (
                        user.settings.get("ui", {})
                        .get("notifications", {})
                        .get("webhook_url", None)
                    )
        except Exception:
            return None

    def get_num_users_active_today(self, db: Optional[Session] = None) -> Optional[int]:
        with get_db_context(db) as db:
            current_timestamp = int(datetime.datetime.now().timestamp())
            today_midnight_timestamp = current_timestamp - (current_timestamp % 86400)
            query = db.query(User).filter(
                User.last_active_at > today_midnight_timestamp
            )
            return query.count()

    def update_user_role_by_id(
        self,
        id: str,
        role: str,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None
                user.role = role
                if role != "admin":
                    self._set_primary_admin_flag(user, False)
                    if not self._find_primary_admin_row(db):
                        candidate = (
                            db.query(User)
                            .filter(User.role == "admin")
                            .order_by(User.created_at)
                            .first()
                        )
                        if candidate:
                            self._set_primary_admin_flag(candidate, True)
                else:
                    if not self._find_primary_admin_row(db):
                        self._set_primary_admin_flag(user, True)

                if commit:
                    db.commit()
                    db.refresh(user)
                else:
                    db.flush()
                return UserModel.model_validate(user)
        except Exception:
            return None

    def update_user_status_by_id(
        self, id: str, form_data: UserStatus, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None
                for key, value in form_data.model_dump(exclude_none=True).items():
                    setattr(user, key, value)
                db.commit()
                db.refresh(user)
                return UserModel.model_validate(user)
        except Exception:
            return None

    def update_user_profile_image_url_by_id(
        self,
        id: str,
        profile_image_url: str,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None
                user.profile_image_url = profile_image_url
                if commit:
                    db.commit()
                    db.refresh(user)
                else:
                    db.flush()
                return UserModel.model_validate(user)
        except Exception:
            return None

    @throttle(DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL)
    def update_last_active_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None
                user.last_active_at = int(time.time())
                db.commit()
                db.refresh(user)
                return UserModel.model_validate(user)
        except Exception:
            return None

    def update_user_oauth_by_id(
        self,
        id: str,
        provider: str,
        sub: str,
        payload: Optional[dict] = None,
        merge: bool = True,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        """
        Update or insert an OAuth provider/sub pair into the user's oauth JSON field.
        Example resulting structure:
            {
                "google": { "sub": "123" },
                "github": { "sub": "abc" }
            }
        """
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None

                # Load existing oauth JSON or create empty
                oauth = user.oauth or {}

                # Update or insert provider entry, preserving existing payload when requested
                provider_entry = oauth.get(provider, {}) if merge else {}
                if payload and isinstance(payload, dict):
                    provider_entry.update(payload)
                provider_entry["sub"] = sub
                oauth[provider] = provider_entry

                # Persist updated JSON
                db.query(User).filter_by(id=id).update({"oauth": oauth})
                if commit:
                    db.commit()
                else:
                    db.flush()

                return UserModel.model_validate(user)

        except Exception:
            return None

    def update_user_scim_by_id(
        self,
        id: str,
        provider: str,
        external_id: str,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        """
        Update or insert a SCIM provider/external_id pair into the user's scim JSON field.
        Example resulting structure:
            {
                "microsoft": { "external_id": "abc" },
                "okta": { "external_id": "def" }
            }
        """
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None

                scim = user.scim or {}
                scim[provider] = {"external_id": external_id}

                db.query(User).filter_by(id=id).update({"scim": scim})
                if commit:
                    db.commit()
                else:
                    db.flush()

                return UserModel.model_validate(user)

        except Exception:
            return None

    def update_user_by_id(
        self,
        id: str,
        updated: dict,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None
                role_update = updated.get("role")
                for key, value in updated.items():
                    setattr(user, key, value)
                if role_update is not None:
                    if role_update != "admin":
                        self._set_primary_admin_flag(user, False)
                        if not self._find_primary_admin_row(db):
                            candidate = (
                                db.query(User)
                                .filter(User.role == "admin")
                                .order_by(User.created_at)
                                .first()
                            )
                            if candidate:
                                self._set_primary_admin_flag(candidate, True)
                    else:
                        if not self._find_primary_admin_row(db):
                            self._set_primary_admin_flag(user, True)

                if commit:
                    db.commit()
                    db.refresh(user)
                else:
                    db.flush()
                return UserModel.model_validate(user)
        except Exception as e:
            print(e)
            return None

    def update_user_settings_by_id(
        self,
        id: str,
        updated: dict,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> Optional[UserModel]:
        try:
            with get_db_context(db) as db:
                user = db.query(User).filter_by(id=id).first()
                if not user:
                    return None

                user_settings = user.settings

                if user_settings is None:
                    user_settings = {}

                user_settings.update(updated)

                db.query(User).filter_by(id=id).update({"settings": user_settings})
                if commit:
                    db.commit()
                else:
                    db.flush()

                user = db.query(User).filter_by(id=id).first()
                return UserModel.model_validate(user)
        except Exception:
            return None

    def delete_user_by_id(
        self, id: str, db: Optional[Session] = None, commit: bool = True
    ) -> bool:
        try:
            with get_db_context(db) as db:
                group_ids = [
                    group_id
                    for (group_id,) in db.query(GroupMember.group_id)
                    .filter(GroupMember.user_id == id)
                    .distinct()
                    .all()
                ]
                if group_ids:
                    db.query(GroupMember).filter(
                        GroupMember.user_id == id,
                        GroupMember.group_id.in_(group_ids),
                    ).delete(synchronize_session=False)
                    db.query(Group).filter(Group.id.in_(group_ids)).update(
                        {"updated_at": int(time.time())},
                        synchronize_session=False,
                    )

                result = Chats.delete_chats_by_user_id(id, db=db, commit=False)
                if not result:
                    db.rollback()
                    return False

                db.query(User).filter_by(id=id).delete()
                if commit:
                    db.commit()
                else:
                    db.flush()

                return True
        except Exception:
            return False

    def get_user_api_key_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[str]:
        try:
            with get_db_context(db) as db:
                api_key = db.query(ApiKey).filter_by(user_id=id).first()
                return api_key.key if api_key else None
        except Exception:
            return None

    def update_user_api_key_by_id(
        self,
        id: str,
        api_key: str,
        db: Optional[Session] = None,
        commit: bool = True,
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(ApiKey).filter_by(user_id=id).delete()

                now = int(time.time())
                new_api_key = ApiKey(
                    id=f"key_{id}",
                    user_id=id,
                    key=api_key,
                    created_at=now,
                    updated_at=now,
                )
                db.add(new_api_key)
                if commit:
                    db.commit()
                else:
                    db.flush()

                return True

        except Exception:
            return False

    def delete_user_api_key_by_id(self, id: str, db: Optional[Session] = None) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(ApiKey).filter_by(user_id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def get_valid_user_ids(
        self, user_ids: list[str], db: Optional[Session] = None
    ) -> list[str]:
        with get_db_context(db) as db:
            users = db.query(User).filter(User.id.in_(user_ids)).all()
            return [user.id for user in users]

    def get_super_admin_user(self, db: Optional[Session] = None) -> Optional[UserModel]:
        return self.ensure_primary_admin(db=db, commit=True)

    def get_active_user_count(self, db: Optional[Session] = None) -> int:
        with get_db_context(db) as db:
            # Consider user active if last_active_at within the last 3 minutes
            three_minutes_ago = int(time.time()) - 180
            count = (
                db.query(User).filter(User.last_active_at >= three_minutes_ago).count()
            )
            return count

    @staticmethod
    def is_active(user: UserModel) -> bool:
        """Compute active status from an already-loaded UserModel (no DB hit)."""
        if user.last_active_at:
            three_minutes_ago = int(time.time()) - 180
            return user.last_active_at >= three_minutes_ago
        return False

    def is_user_active(self, user_id: str, db: Optional[Session] = None) -> bool:
        with get_db_context(db) as db:
            user = db.query(User).filter_by(id=user_id).first()
            if user and user.last_active_at:
                # Consider user active if last_active_at within the last 3 minutes
                three_minutes_ago = int(time.time()) - 180
                return user.last_active_at >= three_minutes_ago
            return False


Users = UsersTable()
