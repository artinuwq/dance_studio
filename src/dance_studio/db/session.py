from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dance_studio.core.config import DATABASE_URL

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


def get_session():
    return Session()
