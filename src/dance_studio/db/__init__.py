import logging

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from dance_studio.core.media_manager import create_required_directories
from dance_studio.db.models import Base, Staff, User
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID, ENV, AUTO_CREATE_SCHEMA, DATABASE_URL

logger = logging.getLogger(__name__)

if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL environment variable is required')

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    pool_size=5,
    max_overflow=10,
)

Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

REQUIRED_TABLES_DEV = {
    'users',
    'staff',
    'schedule',
    'news',
    'directions',
    'groups',
}


def ensure_schema_dev() -> None:
    """Auto-create schema in development for empty databases.

    If schema exists but misses required core tables, fail fast with explicit error.
    """
    should_autocreate = ENV == 'dev' or AUTO_CREATE_SCHEMA
    if not should_autocreate:
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if not table_names:
        create_required_directories()
        Base.metadata.create_all(engine)
        logger.info('[db] Database schema created from SQLAlchemy metadata')
        return

    missing_tables = REQUIRED_TABLES_DEV - table_names
    if missing_tables:
        missing_list = ', '.join(sorted(missing_tables))
        raise RuntimeError(
            f'Database schema is partially initialized. Missing required tables: {missing_list}. '
            'For development, use an empty DB for auto-create or fix schema manually.'
        )


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


def get_session():
    return Session()


__all__ = [
    'engine',
    'Session',
    'ensure_schema_dev',
    'bootstrap_data',
    'get_session',
]
