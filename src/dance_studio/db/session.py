from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Pull DATABASE_URL from central settings to ensure the .env loader runs
# before we touch the database configuration.
from dance_studio.core.settings import (
    DATABASE_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE_SECONDS,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_TIMEOUT_SECONDS,
    DATABASE_URL,
)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    pool_size=DATABASE_POOL_SIZE,
    max_overflow=DATABASE_MAX_OVERFLOW,
    pool_timeout=DATABASE_POOL_TIMEOUT_SECONDS,
    pool_recycle=DATABASE_POOL_RECYCLE_SECONDS,
)

Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session():
    return Session()
