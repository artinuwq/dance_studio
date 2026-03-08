from dance_studio.core.booking_utils import (
    _format_duration,
    build_booking_keyboard_data,
    parse_bundle_group_ids,
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
    kb = build_booking_keyboard_data("created", "group", 123, is_free_group_trial=False)
    assert any("request_payment" in btn["callback_data"] for row in kb for btn in row)


def test_build_keyboard_free_trial():
    kb = build_booking_keyboard_data("created", "group", 123, is_free_group_trial=True)
    assert any("approve" in btn["callback_data"] for row in kb for btn in row)


def test_build_keyboard_has_no_attendance_or_cancel_actions_for_tg():
    variants = [
        build_booking_keyboard_data("created", "group", 123, is_free_group_trial=False),
        build_booking_keyboard_data("waiting_payment", "group", 123, is_free_group_trial=False),
        build_booking_keyboard_data("confirmed", "group", 123, is_free_group_trial=False),
        build_booking_keyboard_data("created", "individual", 123),
        build_booking_keyboard_data("waiting_payment", "individual", 123),
        build_booking_keyboard_data("confirmed", "individual", 123),
    ]
    callback_actions = {
        button["callback_data"].split(":")[-1]
        for keyboard in variants
        for row in keyboard
        for button in row
    }
    assert "attended" not in callback_actions
    assert "no_show" not in callback_actions
    assert "cancel" not in callback_actions
