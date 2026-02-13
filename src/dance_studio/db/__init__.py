import logging
from pathlib import Path

from dance_studio.db.models import Staff, User
from dance_studio.db.session import Session, engine, get_session
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID, MIGRATE_ON_START

logger = logging.getLogger(__name__)


def _alembic_config():
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[3]
    alembic_ini = project_root / 'alembic.ini'
    alembic_dir = project_root / 'alembic'

    config = Config(str(alembic_ini))
    config.set_main_option('script_location', str(alembic_dir))
    return config


def ensure_db_schema() -> None:
    """Run Alembic migrations up to head when enabled by config."""
    if not MIGRATE_ON_START:
        return

    from alembic import command

    try:
        command.upgrade(_alembic_config(), 'head')
        logger.info('[db] Alembic migrations applied successfully')
    except Exception as exc:
        raise RuntimeError(
            'Failed to apply database migrations (alembic upgrade head). '
            'Schema may be partial/corrupted. Check migration history and DB state.'
        ) from exc


def bootstrap_data() -> None:
    """
    Ensures technical admin and owners exist in the DB.
    If absent — creates; if present — updates positions/names from user profiles.
    """
    db = Session()

    try:
        if TECH_ADMIN_ID:
            tech_admin = db.query(Staff).filter_by(telegram_id=TECH_ADMIN_ID).first()
            tech_admin_name = 'Технический админ'

            user = db.query(User).filter_by(telegram_id=TECH_ADMIN_ID).first()
            if user and user.name:
                tech_admin_name = user.name

            if not tech_admin:
                tech_admin = Staff(
                    name=tech_admin_name,
                    phone=None,
                    telegram_id=TECH_ADMIN_ID,
                    position='тех. админ',
                    status='active',
                )
                db.add(tech_admin)
                logger.info('[db] Created technical admin (ID: %s, name: %s)', TECH_ADMIN_ID, tech_admin_name)
            else:
                if tech_admin.position != 'тех. админ':
                    tech_admin.position = 'тех. админ'
                    logger.info('[db] Updated technical admin position')
                if (not tech_admin.name or tech_admin.name.strip() == '') and user and user.name:
                    tech_admin.name = tech_admin_name
                    logger.info('[db] Filled technical admin name from profile')

        for idx, owner_id in enumerate(OWNER_IDS, 1):
            owner = db.query(Staff).filter_by(telegram_id=owner_id).first()
            owner_name = f'Владелец {idx}' if len(OWNER_IDS) > 1 else 'Владелец'

            user = db.query(User).filter_by(telegram_id=owner_id).first()
            if user and user.name:
                owner_name = user.name

            if not owner:
                owner = Staff(
                    name=owner_name,
                    phone=None,
                    telegram_id=owner_id,
                    position='владелец',
                    status='active',
                )
                db.add(owner)
                logger.info('[db] Created owner (ID: %s, name: %s)', owner_id, owner_name)
            else:
                if owner.position != 'владелец':
                    owner.position = 'владелец'
                    logger.info('[db] Updated owner position (ID: %s)', owner_id)
                if (not owner.name or owner.name.strip() == '') and user and user.name:
                    owner.name = owner_name
                    logger.info('[db] Filled owner name from profile (ID: %s)', owner_id)

        db.commit()
        logger.info('[db] Staff initialization complete')
    except Exception:
        db.rollback()
        logger.exception('[db] Error during staff initialization')
        raise
    finally:
        db.close()


__all__ = [
    'engine',
    'Session',
    'ensure_db_schema',
    'bootstrap_data',
    'get_session',
]
