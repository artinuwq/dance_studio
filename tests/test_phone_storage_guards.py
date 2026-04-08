from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dance_studio.core.time import utcnow
from dance_studio.db.models import Base, PhoneVerificationCode, User, UserPhone


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_user_phone_fields_are_normalized_before_commit():
    session_factory = _session_factory()
    db = session_factory()

    user = User(name="Phone Guard", phone="8 (999) 111-22-33", primary_phone="9991112233")
    db.add(user)
    db.commit()

    db.refresh(user)
    assert user.phone == "+79991112233"
    assert user.primary_phone == "+79991112233"


def test_user_phone_row_and_otp_phone_are_normalized_before_commit():
    session_factory = _session_factory()
    db = session_factory()

    user = User(name="Phone Guard 2")
    db.add(user)
    db.commit()

    phone_row = UserPhone(user_id=user.id, phone_e164="8 (999) 222-33-44", source="telegram")
    code = PhoneVerificationCode(
        phone="9992223344",
        code_hash="hash",
        purpose="login",
        expires_at=utcnow() + timedelta(minutes=5),
    )
    db.add_all([phone_row, code])
    db.commit()

    db.refresh(phone_row)
    db.refresh(code)
    assert phone_row.phone_e164 == "+79992223344"
    assert code.phone == "+79992223344"
