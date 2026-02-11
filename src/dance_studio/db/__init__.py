import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from dance_studio.core.media_manager import create_required_directories
from dance_studio.db.models import Base, Staff, User
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID

# Project root: .../dance_studio
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VAR_ROOT = PROJECT_ROOT / "var"
DB_DIR = VAR_ROOT / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "dance.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

Session = sessionmaker(bind=engine)


def _ensure_booking_request_columns():
    required_columns = {
        "group_id": "INTEGER",
        "lessons_count": "INTEGER",
        "teacher_id": "INTEGER",
        "group_start_date": "DATE",
        "valid_until": "DATE",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(booking_requests)"))}
        for name, data_type in required_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE booking_requests ADD COLUMN {name} {data_type}"))


def _ensure_individual_lesson_columns():
    required_columns = {
        "booking_id": "INTEGER",
        "status": "TEXT",
        "status_updated_at": "DATETIME",
        "status_updated_by_id": "INTEGER",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(individual_lessons)"))}
        for name, data_type in required_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE individual_lessons ADD COLUMN {name} {data_type}"))


def _ensure_direction_type_columns():
    with engine.begin() as conn:
        directions_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(directions)"))}
        if "direction_type" not in directions_cols:
            conn.execute(text("ALTER TABLE directions ADD COLUMN direction_type TEXT DEFAULT 'dance'"))
        conn.execute(text("UPDATE directions SET direction_type = 'dance' WHERE direction_type IS NULL OR direction_type = ''"))

        upload_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(direction_upload_sessions)"))}
        if "direction_type" not in upload_cols:
            conn.execute(text("ALTER TABLE direction_upload_sessions ADD COLUMN direction_type TEXT DEFAULT 'dance'"))
        conn.execute(text("UPDATE direction_upload_sessions SET direction_type = 'dance' WHERE direction_type IS NULL OR direction_type = ''"))


def init_db():
    """Create DB schema and ensure required columns/admin users exist."""
    create_required_directories()
    Base.metadata.create_all(engine)
    _ensure_booking_request_columns()
    _ensure_individual_lesson_columns()
    _ensure_direction_type_columns()
    _init_admin_and_owner()


def _init_admin_and_owner():
    """
    Ensures technical admin and owners exist in the DB.
    If absent — creates; if present — updates positions/names from user profiles.
    """
    db = Session()

    try:
        if TECH_ADMIN_ID:
            tech_admin = db.query(Staff).filter_by(telegram_id=TECH_ADMIN_ID).first()
            tech_admin_name = "Технический админ"

            user = db.query(User).filter_by(telegram_id=TECH_ADMIN_ID).first()
            if user and user.name:
                tech_admin_name = user.name

            if not tech_admin:
                tech_admin = Staff(
                    name=tech_admin_name,
                    phone=None,
                    telegram_id=TECH_ADMIN_ID,
                    position="тех. админ",
                    status="active",
                )
                db.add(tech_admin)
                print(f"[db] Created technical admin (ID: {TECH_ADMIN_ID}, name: {tech_admin_name})")
            else:
                if tech_admin.position != "тех. админ":
                    tech_admin.position = "тех. админ"
                    print("[db] Updated technical admin position")
                if (not tech_admin.name or tech_admin.name.strip() == "") and user and user.name:
                    tech_admin.name = tech_admin_name
                    print("[db] Filled technical admin name from profile")

        for idx, owner_id in enumerate(OWNER_IDS, 1):
            owner = db.query(Staff).filter_by(telegram_id=owner_id).first()
            owner_name = f"Владелец {idx}" if len(OWNER_IDS) > 1 else "Владелец"

            user = db.query(User).filter_by(telegram_id=owner_id).first()
            if user and user.name:
                owner_name = user.name

            if not owner:
                owner = Staff(
                    name=owner_name,
                    phone=None,
                    telegram_id=owner_id,
                    position="владелец",
                    status="active",
                )
                db.add(owner)
                print(f"[db] Created owner (ID: {owner_id}, name: {owner_name})")
            else:
                if owner.position != "владелец":
                    owner.position = "владелец"
                    print(f"[db] Updated owner position (ID: {owner_id})")
                if (not owner.name or owner.name.strip() == "") and user and user.name:
                    owner.name = owner_name
                    print(f"[db] Filled owner name from profile (ID: {owner_id})")

        db.commit()
        print("[db] Staff initialization complete")
    except Exception as e:
        db.rollback()
        print(f"[db] Error during staff initialization: {e}")
    finally:
        db.close()


def get_session():
    return Session()


__all__ = [
    "BASE_DIR",
    "DB_PATH",
    "engine",
    "Session",
    "init_db",
    "get_session",
]
