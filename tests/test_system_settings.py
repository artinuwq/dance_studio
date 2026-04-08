import pytest
from dance_studio.core.system_settings_service import (
    SETTING_SPECS,
    _coerce_bool, 
    _coerce_int, 
    _normalize_telegram_username,
    SettingValidationError
)

def test_coerce_bool():
    assert _coerce_bool("true") is True
    assert _coerce_bool("ON") is True
    assert _coerce_bool(1) is True
    assert _coerce_bool("false") is False
    assert _coerce_bool(0) is False
    with pytest.raises(SettingValidationError):
        _coerce_bool("maybe")

def test_coerce_int():
    assert _coerce_int("123") == 123
    assert _coerce_int(456) == 456
    with pytest.raises(SettingValidationError):
        _coerce_int("abc")
    with pytest.raises(SettingValidationError):
        _coerce_int(True) # bool is not allowed as int in this logic

def test_normalize_telegram_username():
    assert _normalize_telegram_username("@my_bot") == "@my_bot"
    assert _normalize_telegram_username("  My_Bot  ") == "@my_bot"
    with pytest.raises(SettingValidationError):
        _normalize_telegram_username("bot") # too short (min 5 chars)
    with pytest.raises(SettingValidationError):
        _normalize_telegram_username("Invalid Name!")


def test_runtime_telegram_settings_keys_present():
    expected = {
        "tech.logs_chat_id",
        "tech.backups_topic_id",
        "tech.status_topic_id",
        "tech.critical_topic_id",
        "tech.status_message_id",
        "bookings.admin_chat_id",
    }
    assert expected.issubset(set(SETTING_SPECS.keys()))
