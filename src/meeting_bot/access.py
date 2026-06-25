from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, select

from meeting_bot.config import AppConfig
from meeting_bot.domain import ChatStatus, Role, UserStatus
from meeting_bot.models import AuditLog, Chat, User
from meeting_bot.storage import Database


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class AccessContext:
    user: User
    chat: Chat
    is_new_user: bool
    is_new_chat: bool

    @property
    def approved(self) -> bool:
        return self.user.status == UserStatus.APPROVED.value

    @property
    def can_edit(self) -> bool:
        return (
            self.approved
            and self.user.role in {Role.EDITOR.value, Role.ADMIN.value}
            and not self.chat.read_only
            and self.chat.chat_type == "private"
        )

    @property
    def blocked(self) -> bool:
        return (
            self.user.status == UserStatus.BLOCKED.value
            or self.chat.status == ChatStatus.BLOCKED.value
        )

    @property
    def can_use_llm(self) -> bool:
        return self.approved and not self.blocked and self.chat.status == ChatStatus.APPROVED.value


class AccessService:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self.database = database
        self.config = config

    async def ensure_root_admin(self) -> None:
        admin_id = self.config.telegram.admin_user_id
        async with self.database.session() as session, session.begin():
            user = await session.get(User, admin_id)
            now = now_utc()
            if user is None:
                session.add(
                    User(
                        telegram_user_id=admin_id,
                        username=None,
                        full_name="Root admin",
                        role=Role.ADMIN.value,
                        status=UserStatus.APPROVED.value,
                        created_at=now,
                        updated_at=now,
                        approved_by=admin_id,
                        approved_at=now,
                    )
                )
            else:
                user.role = Role.ADMIN.value
                user.status = UserStatus.APPROVED.value
                user.updated_at = now
                user.approved_by = admin_id
                user.approved_at = user.approved_at or now

    async def observe(
        self,
        *,
        user_id: int,
        username: str | None,
        full_name: str | None,
        chat_id: int,
        chat_type: str,
        chat_title: str | None,
    ) -> AccessContext:
        async with self.database.session() as session, session.begin():
            now = now_utc()
            user = await session.get(User, user_id)
            is_new_user = user is None
            if user is None:
                is_root = user_id == self.config.telegram.admin_user_id
                user = User(
                    telegram_user_id=user_id,
                    username=username,
                    full_name=full_name,
                    role=Role.ADMIN.value if is_root else Role.VIEWER.value,
                    status=UserStatus.APPROVED.value if is_root else UserStatus.PENDING.value,
                    created_at=now,
                    updated_at=now,
                    approved_by=user_id if is_root else None,
                    approved_at=now if is_root else None,
                )
                session.add(user)
                session.add(
                    AuditLog(
                        actor_user_id=user_id,
                        chat_id=chat_id,
                        action="user_registered",
                        target_type="user",
                        target_id=str(user_id),
                        details_json="{}",
                        created_at=now,
                    )
                )
            else:
                user.username = username
                user.full_name = full_name
                user.updated_at = now
                if user_id == self.config.telegram.admin_user_id:
                    user.role = Role.ADMIN.value
                    user.status = UserStatus.APPROVED.value

            chat = await session.get(Chat, chat_id)
            is_new_chat = chat is None
            read_only = chat_type in {"group", "supergroup", "channel"}
            if chat is None:
                chat = Chat(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    title=chat_title,
                    status=ChatStatus.APPROVED.value,
                    read_only=read_only,
                    created_at=now,
                    updated_at=now,
                )
                session.add(chat)
            else:
                chat.chat_type = chat_type
                chat.title = chat_title
                chat.read_only = read_only
                chat.updated_at = now
            await session.flush()
            return AccessContext(user, chat, is_new_user, is_new_chat)

    async def decide_user(
        self, actor_id: int, target_id: int, *, status: str, role: str | None = None
    ) -> User:
        if actor_id != self.config.telegram.admin_user_id:
            raise PermissionError("Только root-admin может управлять доступом.")
        if target_id == self.config.telegram.admin_user_id and status != UserStatus.APPROVED.value:
            raise ValueError("Root-admin нельзя заблокировать или отклонить.")
        if target_id == self.config.telegram.admin_user_id and role not in {
            None,
            Role.ADMIN.value,
        }:
            raise ValueError("Роль root-admin нельзя изменить.")
        if role is not None and role not in {Role.VIEWER.value, Role.EDITOR.value}:
            raise ValueError("Можно назначить только viewer или editor.")
        async with self.database.session() as session, session.begin():
            user = await session.get(User, target_id)
            if user is None:
                raise ValueError("Пользователь не найден.")
            now = now_utc()
            user.status = status
            if role is not None:
                user.role = role
            if status == UserStatus.APPROVED.value:
                user.approved_by = actor_id
                user.approved_at = now
            user.updated_at = now
            session.add(
                AuditLog(
                    actor_user_id=actor_id,
                    chat_id=None,
                    action=f"user_{status}",
                    target_type="user",
                    target_id=str(target_id),
                    details_json=json.dumps({"role": user.role}),
                    created_at=now,
                )
            )
            return user

    async def set_chat_blocked(self, actor_id: int, chat_id: int, blocked: bool) -> Chat:
        if actor_id != self.config.telegram.admin_user_id:
            raise PermissionError("Только root-admin может управлять чатами.")
        async with self.database.session() as session, session.begin():
            chat = await session.get(Chat, chat_id)
            if chat is None:
                raise ValueError("Чат не найден.")
            chat.status = ChatStatus.BLOCKED.value if blocked else ChatStatus.APPROVED.value
            chat.updated_at = now_utc()
            session.add(
                AuditLog(
                    actor_user_id=actor_id,
                    chat_id=chat_id,
                    action="chat_blocked" if blocked else "chat_unblocked",
                    target_type="chat",
                    target_id=str(chat_id),
                    details_json="{}",
                    created_at=now_utc(),
                )
            )
            return chat

    async def users(self) -> list[User]:
        async with self.database.session() as session:
            result = await session.scalars(select(User).order_by(User.status, User.created_at))
            return list(result)

    async def approved_editors(self) -> list[User]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(User).where(
                    User.status == UserStatus.APPROVED.value,
                    User.role.in_([Role.EDITOR.value, Role.ADMIN.value]),
                )
            )
            return list(result)

    async def audit(self, limit: int = 20) -> list[AuditLog]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
            )
            return list(result)

    async def log_blocked_attempt(self, user_id: int, chat_id: int, kind: str) -> None:
        async with self.database.session() as session, session.begin():
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    chat_id=chat_id,
                    action="blocked_attempt",
                    target_type=kind,
                    target_id=None,
                    details_json="{}",
                    created_at=now_utc(),
                )
            )
