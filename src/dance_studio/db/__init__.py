import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from dance_studio.core.media_manager import create_required_directories
from dance_studio.db.models import Base, Staff, User
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID, ENV, AUTO_CREATE_SCHEMA, DATABASE_URL

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


def ensure_schema_dev() -> None:
    """Auto-create schema in development for empty databases."""
    should_autocreate = ENV == 'dev' or AUTO_CREATE_SCHEMA
    if not should_autocreate:
        return

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if not table_names:
        create_required_directories()
        Base.metadata.create_all(engine)
        print('[db] Database schema created from SQLAlchemy metadata')


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
                print(f'[db] Created technical admin (ID: {TECH_ADMIN_ID}, name: {tech_admin_name})')
            else:
                if tech_admin.position != 'тех. админ':
                    tech_admin.position = 'тех. админ'
                    print('[db] Updated technical admin position')
                if (not tech_admin.name or tech_admin.name.strip() == '') and user and user.name:
                    tech_admin.name = tech_admin_name
                    print('[db] Filled technical admin name from profile')

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
                print(f'[db] Created owner (ID: {owner_id}, name: {owner_name})')
            else:
                if owner.position != 'владелец':
                    owner.position = 'владелец'
                    print(f'[db] Updated owner position (ID: {owner_id})')
                if (not owner.name or owner.name.strip() == '') and user and user.name:
                    owner.name = owner_name
                    print(f'[db] Filled owner name from profile (ID: {owner_id})')

        db.commit()
        print('[db] Staff initialization complete')
    except Exception as e:
        db.rollback()
        print(f'[db] Error during staff initialization: {e}')
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
