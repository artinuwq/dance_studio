import pytest
from dance_studio.core.booking_utils import (
    _format_duration, 
    parse_bundle_group_ids, 
    build_booking_keyboard_data
)

def test_format_duration():
    assert _format_duration(60) == "1 час"
    assert _format_duration(120) == "2 часа"
    assert _format_duration(300) == "5 часов"
    assert _format_duration(45) == "45 мин"
    assert _format_duration(65) == "1 час 5 мин"

def test_parse_bundle_group_ids():
    assert parse_bundle_group_ids("[1, 2, 3]") == [1, 2, 3]
    assert parse_bundle_group_ids("invalid") == []
    assert parse_bundle_group_ids(None) == []

def test_build_keyboard_new_group():
    # Для новой платной группы должна быть кнопка запроса оплаты
    kb = build_booking_keyboard_data("NEW", "group", 123, is_free_group_trial=False)
    assert any("request_payment" in btn["callback_data"] for row in kb for btn in row)

def test_build_keyboard_free_trial():
    # Для бесплатного пробного — сразу кнопка подтверждения
    kb = build_booking_keyboard_data("NEW", "group", 123, is_free_group_trial=True)
    assert any("approve" in btn["callback_data"] for row in kb for btn in row)
