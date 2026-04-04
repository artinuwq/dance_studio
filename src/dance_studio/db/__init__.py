import logging
from pathlib import Path

from dance_studio.auth.services.common import resolve_user_id_by_telegram
from dance_studio.db.models import Staff, User
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

    for item in initial_assignments or []:
        telegram_id = int(item.get("telegram_id") or 0)
        if telegram_id <= 0 or telegram_id in seen_telegram_ids:
            continue
        merged.append({
            "telegram_id": telegram_id,
            "position": str(item.get("position") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "status": str(item.get("status") or "active").strip() or "active",
        })
        seen_telegram_ids.add(telegram_id)

    return merged


def _find_staff_record(db, telegram_id: int, resolved_user_id: int | None) -> Staff | None:
    staff = None
    if resolved_user_id:
        staff = db.query(Staff).filter_by(user_id=resolved_user_id).first()
    if not staff:
        staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    return staff


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
            "Schema may be partial/corrupted. Check migration history and DB state."
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
            telegram_id = int(assignment.get("telegram_id") or 0)
            if telegram_id <= 0:
                continue

            resolved_user_id = resolve_user_id_by_telegram(db, telegram_id)
            user = db.query(User).filter_by(id=resolved_user_id).first() if resolved_user_id else None
            staff = _find_staff_record(db, telegram_id=telegram_id, resolved_user_id=resolved_user_id)

            position = str(assignment.get("position") or "").strip()
            status = str(assignment.get("status") or "active").strip() or "active"
            configured_name = str(assignment.get("name") or "").strip()
            resolved_name = (
                (user.name if user and user.name else "")
                or configured_name
                or _default_staff_name(position, index=index, total=len(assignments))
            )

            if not staff:
                staff = Staff(
                    name=resolved_name,
                    phone=None,
                    telegram_id=telegram_id,
                    user_id=resolved_user_id,
                    position=position,
                    status=status,
                )
                db.add(staff)
                logger.info(
                    "[db] Created bootstrap staff (telegram_id=%s, position=%s)",
                    telegram_id,
                    position,
                )
                continue

            changed = False
            if staff.telegram_id != telegram_id:
                staff.telegram_id = telegram_id
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
                    "[db] Updated bootstrap staff (telegram_id=%s, position=%s)",
                    telegram_id,
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
    "ensure_db_schema",
    "bootstrap_data",
    "get_session",
]
