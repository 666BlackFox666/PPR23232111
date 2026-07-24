from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AppUser, AuditLog

ROLE_ADMIN = "admin"
ROLE_CHECKER = "checker"
VALID_ROLES = {ROLE_ADMIN, ROLE_CHECKER}


def parse_admin_telegram_ids() -> set[str]:
    settings = get_settings()
    return {item.strip() for item in settings.admin_telegram_ids.split(",") if item.strip()}


def is_primary_admin(telegram_id: str) -> bool:
    return telegram_id in parse_admin_telegram_ids()


def normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    return username[1:] if username.startswith("@") else username


def normalize_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role}")
    return normalized


def user_display(user: AppUser) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name or user.telegram_id


def is_active_admin_user(user: AppUser | None) -> bool:
    return bool(user and user.is_active and user.role == ROLE_ADMIN)


def add_user_audit(db: Session, action: str, actor: AppUser | None, target: AppUser, comment: str) -> None:
    if actor is None:
        return
    db.add(
        AuditLog(
            action=action,
            user_id=actor.telegram_id,
            user_name=user_display(actor),
            comment=f"telegram_id={target.telegram_id}; {comment}",
        )
    )


def serialize_user(user: AppUser) -> dict:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }


def get_user_by_telegram_id(db: Session, telegram_id: str) -> AppUser | None:
    return db.query(AppUser).filter(AppUser.telegram_id == telegram_id).one_or_none()


def get_or_sync_user(
    db: Session,
    telegram_id: str,
    username: str | None = None,
    full_name: str | None = None,
) -> AppUser | None:
    username = normalize_username(username)
    user = get_user_by_telegram_id(db, telegram_id)
    now = datetime.utcnow()

    if is_primary_admin(telegram_id):
        if user is None:
            user = AppUser(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                role=ROLE_ADMIN,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(user)
        else:
            user.username = username
            user.full_name = full_name
            user.role = ROLE_ADMIN
            user.is_active = True
            user.updated_at = now
        db.commit()
        db.refresh(user)
        return user

    if user is None:
        return None

    changed = False
    if username is not None and user.username != username:
        user.username = username
        changed = True
    if full_name is not None and user.full_name != full_name:
        user.full_name = full_name
        changed = True
    if changed:
        user.updated_at = now
        db.commit()
        db.refresh(user)
    return user


def create_user(
    db: Session,
    telegram_id: str,
    username: str | None,
    full_name: str | None,
    role: str = ROLE_CHECKER,
    is_active: bool = True,
    actor: AppUser | None = None,
) -> AppUser:
    if get_user_by_telegram_id(db, telegram_id):
        raise ValueError("User with this telegram_id already exists")
    role = normalize_role(role)
    if is_primary_admin(telegram_id):
        role = ROLE_ADMIN
        is_active = True
    now = datetime.utcnow()
    user = AppUser(
        telegram_id=telegram_id,
        username=normalize_username(username),
        full_name=full_name,
        role=role,
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    add_user_audit(db, "user_created", actor, user, f"role={role}; active={str(is_active).lower()}")
    db.commit()
    db.refresh(user)
    return user


def update_user(
    db: Session,
    user: AppUser,
    username: str | None = None,
    full_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    actor: AppUser | None = None,
) -> AppUser:
    normalized_role = normalize_role(role) if role is not None else None
    if is_primary_admin(user.telegram_id):
        if normalized_role is not None and normalized_role != ROLE_ADMIN:
            raise ValueError("Primary admin cannot be demoted")
        if is_active is False:
            raise ValueError("Primary admin cannot be disabled")
    if actor and actor.telegram_id == user.telegram_id and is_active is False:
        raise ValueError("You cannot disable yourself without an explicit confirmation")

    changes: list[tuple[str, str]] = []
    if username is not None:
        normalized_username = normalize_username(username)
        if user.username != normalized_username:
            changes.append(("user_updated", "username changed"))
            user.username = normalized_username
    if full_name is not None:
        if user.full_name != full_name:
            changes.append(("user_updated", "full_name changed"))
            user.full_name = full_name
    if normalized_role is not None:
        if user.role != normalized_role:
            changes.append(("user_role_changed", f"role={user.role} -> {normalized_role}"))
            user.role = normalized_role
    if is_active is not None:
        if user.is_active != is_active:
            changes.append(("user_enabled" if is_active else "user_disabled", f"active={str(is_active).lower()}"))
            user.is_active = is_active
    if is_primary_admin(user.telegram_id):
        user.role = ROLE_ADMIN
        user.is_active = True
    user.updated_at = datetime.utcnow()
    for action, comment in changes:
        add_user_audit(db, action, actor, user, comment)
    db.commit()
    db.refresh(user)
    return user


def assign_checker_from_reply(
    db: Session,
    actor: AppUser,
    telegram_id: str,
    username: str | None,
    full_name: str | None,
) -> AppUser:
    """Create or assign a checker from a Telegram reply without enabling disabled users."""
    if not is_active_admin_user(actor):
        raise PermissionError("Требуются права active admin")
    if is_primary_admin(telegram_id):
        raise ValueError("Primary admin cannot be demoted")

    username = normalize_username(username)
    user = get_user_by_telegram_id(db, telegram_id)
    now = datetime.utcnow()
    if user is None:
        user = AppUser(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role=ROLE_CHECKER,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.flush()
    else:
        user.role = ROLE_CHECKER
        if username is not None:
            user.username = username
        if full_name:
            user.full_name = full_name
        user.updated_at = now

    add_user_audit(db, "user_checker_assigned", actor, user, "role=checker; assigned_from_reply")
    db.commit()
    db.refresh(user)
    return user
