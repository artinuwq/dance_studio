import importlib
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dance_studio.core.config as config_module
import dance_studio.core.settings as settings_module
import dance_studio.db as db_module
from dance_studio.db.models import Base, Staff, User
from dance_studio.web.constants import ALLOWED_DIRECTION_TYPES, MAX_UPLOAD_MB


def test_constants():
    assert "dance" in ALLOWED_DIRECTION_TYPES
    assert "sport" in ALLOWED_DIRECTION_TYPES
    assert MAX_UPLOAD_MB == 15


def test_logic_mock():
    result = 100 * 2
    assert result == 200


def test_initial_staff_settings_can_load_json_config_and_derive_legacy_ids():
    config_dir = Path("var")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "_initial_staff_test.json"

    env_backup = {
        "INITIAL_STAFF_CONFIG_PATH": os.environ.get("INITIAL_STAFF_CONFIG_PATH"),
    }

    try:
        config_path.write_text(
            json.dumps(
                {
                    "staff": [
                        {"telegram_id": 111111111, "position": "тех админ"},
                        {"telegram_id": 222222222, "position": "владелец"},
                        {"telegram_id": 333333333, "position": "старший админ"},
                        {"telegram_id": 444444444, "position": "администратор"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        os.environ["INITIAL_STAFF_CONFIG_PATH"] = str(config_path)
        os.environ["OWNER_IDS"] = "999999999"
        os.environ["TECH_ADMIN_ID"] = "888888888"

        reloaded_settings = importlib.reload(settings_module)
        importlib.reload(config_module)

        assert [item["position"] for item in reloaded_settings.INITIAL_STAFF_ASSIGNMENTS] == [
            "тех. админ",
            "владелец",
            "старший админ",
            "администратор",
        ]
        assert reloaded_settings.OWNER_IDS == [222222222]
        assert reloaded_settings.TECH_ADMIN_ID == 111111111
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("OWNER_IDS", None)
        os.environ.pop("TECH_ADMIN_ID", None)
        importlib.reload(settings_module)
        importlib.reload(config_module)
        if config_path.exists():
            config_path.unlink()


def test_bootstrap_data_creates_initial_staff_from_assignments_and_is_idempotent(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        db.add(User(name="Existing Owner", telegram_id=222222222))
        db.add(Staff(name="", telegram_id=333333333, position="модератор", status="dismissed"))
        db.commit()

    assignments = [
        {"telegram_id": 111111111, "position": "тех. админ", "name": "", "status": "active"},
        {"telegram_id": 222222222, "position": "владелец", "name": "", "status": "active"},
        {"telegram_id": 333333333, "position": "старший админ", "name": "Старший админ", "status": "active"},
        {"telegram_id": 444444444, "position": "администратор", "name": "Администратор", "status": "active"},
    ]

    monkeypatch.setattr(db_module, "Session", session_factory)
    monkeypatch.setattr(db_module, "_runtime_config", lambda: (assignments, False))

    db_module.bootstrap_data()
    db_module.bootstrap_data()

    with session_factory() as db:
        rows = db.query(Staff).order_by(Staff.telegram_id.asc()).all()
        assert len(rows) == 4

        tech_admin = next(row for row in rows if row.telegram_id == 111111111)
        owner = next(row for row in rows if row.telegram_id == 222222222)
        senior_admin = next(row for row in rows if row.telegram_id == 333333333)
        admin = next(row for row in rows if row.telegram_id == 444444444)

        assert tech_admin.position == "тех. админ"
        assert tech_admin.user_id is None
        assert tech_admin.name == "Технический админ"

        assert owner.position == "владелец"
        assert owner.user is not None
        assert owner.user.telegram_id == 222222222
        assert owner.name == "Existing Owner"

        assert senior_admin.position == "старший админ"
        assert senior_admin.status == "active"
        assert senior_admin.name == "Старший админ"

        assert admin.position == "администратор"
        assert admin.name == "Администратор"
