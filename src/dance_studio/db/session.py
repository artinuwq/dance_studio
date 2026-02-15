from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Pull DATABASE_URL from central settings to ensure the .env loader runs
# before we touch the database configuration.
from dance_studio.core.settings import DATABASE_URL

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

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
