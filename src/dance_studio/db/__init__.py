import logging
from pathlib import Path

from dance_studio.auth.services.common import (
    get_verified_phone_user,
    normalize_phone_e164,
    resolve_user_id_by_telegram,
)
from dance_studio.db.models import Staff, User, UserPhone
from dance_studio.db.session import Session, engine, get_session

logger = logging.getLogger(__name__)


def _runtime_config():
    from dance_studio.core.config import (
        INITIAL_STAFF_ASSIGNMENTS,
        MIGRATE_ON_START,
    )

    return INITIAL_STAFF_ASSIGNMENTS, MIGRATE_ON_START


def _default_staff_name(position: str, index: int | None = None, total: int | None = None) -> str:
    if position == "тех. админ":
        return "Технический админ"
    if position == "владелец":
        if total and total > 1 and index:
            return f"Владелец {index}"
        return "Владелец"
    if position == "старший админ":
        return "Старший админ"
    if position == "администратор":
        return "Администратор"
    if position == "модератор":
        return "Модератор"
    if position == "учитель":
        return "Учитель"
    return "Сотрудник"


def _merge_bootstrap_staff_assignments(initial_assignments: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen_telegram_ids: set[int] = set()
    seen_phones: set[str] = set()

    for item in initial_assignments or []:
        try:
            telegram_id = int(item.get("telegram_id") or 0)
        except (TypeError, ValueError):
            telegram_id = 0
        if telegram_id <= 0:
            telegram_id = None
        phone = normalize_phone_e164(item.get("phone"))
        if telegram_id is None and phone is None:
            continue
        if telegram_id is not None and telegram_id in seen_telegram_ids:
            continue
        if phone is not None and phone in seen_phones:
            continue
        merged.append({
            "telegram_id": telegram_id,
            "phone": phone,
            "position": str(item.get("position") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "status": str(item.get("status") or "active").strip() or "active",
        })
        if telegram_id is not None:
            seen_telegram_ids.add(telegram_id)
        if phone is not None:
            seen_phones.add(phone)

    return merged


def _default_staff_user_name(staff_name: str | None, telegram_id: int | None) -> str:
    resolved_name = str(staff_name or "").strip()
    if resolved_name:
        return resolved_name
    if telegram_id:
        return f"Staff {telegram_id}"
    return "Staff"


def _resolve_staff_user(db, *, user_id: int | None = None, telegram_id: int | None = None) -> User | None:
    user = None
    if user_id:
        user = db.query(User).filter_by(id=user_id).first()
    if not user and telegram_id:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
    return user


def ensure_staff_user_link(db, staff: Staff, *, commit: bool = False) -> bool:
    if not staff or staff.user_id:
        return False

    telegram_id = getattr(staff, "telegram_id", None)
    try:
        telegram_id = int(telegram_id) if telegram_id is not None else None
    except (TypeError, ValueError):
        telegram_id = None
    if not telegram_id:
        return False

    user = _resolve_staff_user(db, telegram_id=telegram_id)
    if not user:
        user = User(
            telegram_id=telegram_id,
            name=_default_staff_user_name(staff.name, telegram_id),
        )
        db.add(user)
        db.flush()
    elif not str(user.name or "").strip():
        user.name = _default_staff_user_name(staff.name, telegram_id)

    staff.user_id = user.id
    db.flush()
    if commit:
        db.commit()
    return True


def normalize_staff_user_links(
    db,
    *,
    user_id: int | None = None,
    telegram_id: int | None = None,
    commit: bool = False,
) -> bool:
    resolved_user = _resolve_staff_user(db, user_id=user_id, telegram_id=telegram_id)
    resolved_telegram_id = telegram_id
    if resolved_telegram_id is None and resolved_user and resolved_user.telegram_id is not None:
        try:
            resolved_telegram_id = int(resolved_user.telegram_id)
        except (TypeError, ValueError):
            resolved_telegram_id = None

    if not resolved_telegram_id:
        return False

    changed = False
    legacy_staff_rows = db.query(Staff).filter_by(telegram_id=resolved_telegram_id).all()
    for staff in legacy_staff_rows:
        if staff.user_id:
            continue
        if resolved_user:
            staff.user_id = resolved_user.id
            db.flush()
            changed = True
            continue
        if ensure_staff_user_link(db, staff):
            resolved_user = _resolve_staff_user(db, user_id=staff.user_id)
            changed = True

    if commit and changed:
        db.commit()
    return changed


def _find_staff_record(
    db,
    *,
    telegram_id: int | None = None,
    phone_e164: str | None = None,
    resolved_user_id: int | None = None,
) -> Staff | None:
    staff = None
    if resolved_user_id:
        staff = db.query(Staff).filter_by(user_id=resolved_user_id).first()
    if not staff and telegram_id:
        legacy_staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
        if legacy_staff:
            ensure_staff_user_link(db, legacy_staff)
            staff = legacy_staff
    if not staff and phone_e164:
        staff = db.query(Staff).filter(Staff.phone == phone_e164).order_by(Staff.id.asc()).first()
    return staff


def _resolve_user_by_verified_phone(db, *, phone_e164: str | None) -> User | None:
    user, matched_user_ids = get_verified_phone_user(db, phone_e164=phone_e164)
    if len(matched_user_ids) > 1:
        logger.warning("[db] Skipped bootstrap phone assignment for conflicted phone %s", phone_e164)
        return None
    return user


def _verified_user_phones(db, *, user_id: int) -> list[str]:
    rows = (
        db.query(UserPhone)
        .filter(UserPhone.user_id == user_id, UserPhone.verified_at.isnot(None))
        .order_by(UserPhone.is_primary.desc(), UserPhone.id.asc())
        .all()
    )
    phones: list[str] = []
    seen: set[str] = set()
    for row in rows:
        phone_e164 = normalize_phone_e164(getattr(row, "phone_e164", None))
        if not phone_e164 or phone_e164 in seen:
            continue
        phones.append(phone_e164)
        seen.add(phone_e164)
    return phones


def sync_bootstrap_staff_assignment_for_user(db, *, user_id: int, commit: bool = False) -> bool:
    user = _resolve_staff_user(db, user_id=user_id)
    if not user or getattr(user, "is_archived", False):
        return False

    initial_assignments, _ = _runtime_config()
    assignments = _merge_bootstrap_staff_assignments(initial_assignments)
    if not assignments:
        return False

    verified_phones = _verified_user_phones(db, user_id=user.id)
    try:
        user_telegram_id = int(user.telegram_id) if user.telegram_id is not None else None
    except (TypeError, ValueError):
        user_telegram_id = None

    matched_assignment = None
    matched_index = 0
    for index, assignment in enumerate(assignments, start=1):
        assignment_telegram_id = assignment.get("telegram_id")
        assignment_phone = assignment.get("phone")
        if user_telegram_id and assignment_telegram_id == user_telegram_id:
            matched_assignment = assignment
            matched_index = index
            break
        if assignment_phone and assignment_phone in verified_phones:
            matched_assignment = assignment
            matched_index = index
            break

    if not matched_assignment:
        return False

    assignment_phone = matched_assignment.get("phone")
    position = str(matched_assignment.get("position") or "").strip()
    status = str(matched_assignment.get("status") or "active").strip() or "active"
    configured_name = str(matched_assignment.get("name") or "").strip()
    resolved_name = (
        configured_name
        or (user.name if user.name else "")
        or _default_staff_name(position, index=matched_index, total=len(assignments))
    )
    staff = _find_staff_record(
        db,
        telegram_id=user_telegram_id,
        phone_e164=assignment_phone,
        resolved_user_id=user.id,
    )

    if staff and staff.user_id not in (None, user.id):
        logger.warning(
            "[db] Skipped bootstrap staff sync for user_id=%s because staff_id=%s belongs to user_id=%s",
            user.id,
            staff.id,
            staff.user_id,
        )
        return False

    changed = False
    if not staff:
        staff = Staff(
            name=resolved_name,
            phone=assignment_phone or (verified_phones[0] if verified_phones else None),
            telegram_id=user_telegram_id,
            user_id=user.id,
            position=position,
            status=status,
        )
        db.add(staff)
        db.flush()
        changed = True
        logger.info("[db] Created bootstrap staff for user_id=%s from config assignment", user.id)
    else:
        if staff.user_id != user.id:
            staff.user_id = user.id
            changed = True
        if assignment_phone and staff.phone != assignment_phone:
            staff.phone = assignment_phone
            changed = True
        elif not staff.phone and verified_phones:
            staff.phone = verified_phones[0]
            changed = True
        if user_telegram_id and staff.telegram_id != user_telegram_id:
            staff.telegram_id = user_telegram_id
            changed = True
        if position and staff.position != position:
            staff.position = position
            changed = True
        if status and staff.status != status:
            staff.status = status
            changed = True
        if (not staff.name or not staff.name.strip()) and resolved_name:
            staff.name = resolved_name
            changed = True
        if changed:
            logger.info("[db] Updated bootstrap staff for user_id=%s from config assignment", user.id)

    if commit and changed:
        db.commit()
    return changed


def _alembic_config():
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[3]
    alembic_ini = project_root / "alembic.ini"
    alembic_dir = project_root / "alembic"

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(alembic_dir))
    return config


def ensure_db_schema() -> None:
    """Run Alembic migrations up to head when enabled by config."""
    _, migrate_on_start = _runtime_config()
    if not migrate_on_start:
        return

    from alembic import command

    try:
        command.upgrade(_alembic_config(), "head")
        logger.info("[db] Alembic migrations applied successfully")
    except Exception as exc:
        raise RuntimeError(
            "Failed to apply database migrations (alembic upgrade head). "
            f"Schema may be partial/corrupted. Check migration history and DB state. Original error: {exc}"
        ) from exc


def bootstrap_data() -> None:
    """
    Ensure bootstrap staff assignments exist in the DB from JSON config.
    """
    initial_assignments, _ = _runtime_config()
    assignments = _merge_bootstrap_staff_assignments(initial_assignments)
    db = Session()

    try:
        for index, assignment in enumerate(assignments, start=1):
            telegram_id = assignment.get("telegram_id")
            phone_e164 = normalize_phone_e164(assignment.get("phone"))
            if not telegram_id and not phone_e164:
                continue

            if telegram_id:
                normalize_staff_user_links(db, telegram_id=telegram_id)
            resolved_user_id = resolve_user_id_by_telegram(db, telegram_id) if telegram_id else None
            user = db.query(User).filter_by(id=resolved_user_id).first() if resolved_user_id else None
            if not user and phone_e164:
                user = _resolve_user_by_verified_phone(db, phone_e164=phone_e164)
                resolved_user_id = user.id if user else None
            if user and not telegram_id:
                try:
                    telegram_id = int(user.telegram_id) if user.telegram_id is not None else None
                except (TypeError, ValueError):
                    telegram_id = None
            staff = _find_staff_record(
                db,
                telegram_id=telegram_id,
                phone_e164=phone_e164,
                resolved_user_id=resolved_user_id,
            )

            position = str(assignment.get("position") or "").strip()
            status = str(assignment.get("status") or "active").strip() or "active"
            configured_name = str(assignment.get("name") or "").strip()
            resolved_name = (
                configured_name
                or (user.name if user and user.name else "")
                or _default_staff_name(position, index=index, total=len(assignments))
            )

            if not staff:
                staff = Staff(
                    name=resolved_name,
                    phone=phone_e164,
                    telegram_id=telegram_id,
                    user_id=resolved_user_id,
                    position=position,
                    status=status,
                )
                db.add(staff)
                logger.info(
                    "[db] Created bootstrap staff (telegram_id=%s, phone=%s, position=%s)",
                    telegram_id,
                    phone_e164,
                    position,
                )
                continue

            changed = False
            if telegram_id and staff.telegram_id != telegram_id:
                staff.telegram_id = telegram_id
                changed = True
            if phone_e164 and staff.phone != phone_e164:
                staff.phone = phone_e164
                changed = True
            if resolved_user_id and staff.user_id != resolved_user_id:
                staff.user_id = resolved_user_id
                changed = True
            if position and staff.position != position:
                staff.position = position
                changed = True
            if status and staff.status != status:
                staff.status = status
                changed = True
            if (not staff.name or not staff.name.strip()) and resolved_name:
                staff.name = resolved_name
                changed = True

            if changed:
                logger.info(
                    "[db] Updated bootstrap staff (telegram_id=%s, phone=%s, position=%s)",
                    telegram_id,
                    phone_e164,
                    position,
                )

        db.commit()
        logger.info("[db] Staff initialization complete")
    except Exception:
        db.rollback()
        logger.exception("[db] Error during staff initialization")
        raise
    finally:
        db.close()


__all__ = [
    "engine",
    "Session",
    "ensure_staff_user_link",
    "ensure_db_schema",
    "bootstrap_data",
    "get_session",
    "normalize_staff_user_links",
    "sync_bootstrap_staff_assignment_for_user",
]
