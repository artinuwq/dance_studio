from datetime import date
from types import SimpleNamespace

from dance_studio.core.booking_utils import (
    _format_duration,
    build_booking_keyboard_data,
    format_booking_message,
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


def test_format_group_booking_message_uses_new_multibundle_layout():
    direction = SimpleNamespace(title="Классическая хореография", base_price=1000)
    teacher = SimpleNamespace(name="Кристина Кислова")
    group_main = SimpleNamespace(
        id=25,
        name="КЛАССИЧЕСКАЯ ХОРЕОГРАФИЯ (взрослые 14+)",
        direction=direction,
        teacher=teacher,
    )
    group_extra = SimpleNamespace(
        id=20,
        name="РАСТЯЖКА",
        direction=SimpleNamespace(title="Растяжка", base_price=1000),
        teacher=SimpleNamespace(name="Анна Петрова"),
    )
    booking = SimpleNamespace(
        status="waiting_payment",
        object_type="group",
        user_name="•-•",
        user_username="ByteArcVoid",
        user_telegram_id=1173610489,
        comment=None,
        date=None,
        time_from=None,
        time_to=None,
        duration_minutes=None,
        overlaps_json=None,
        status_updated_at=None,
        group=group_main,
        group_id=25,
        abonement_type="multi",
        bundle_group_ids_json="[25, 20]",
        bundle_groups=[group_main, group_extra],
        lessons_count=16,
        group_start_date=date(2026, 3, 17),
        valid_until=date(2026, 4, 16),
        requested_amount=12800,
        requested_currency="RUB",
    )

    text = format_booking_message(booking)

    assert "• Контакт: @ByteArcVoid • <a href=\"tg://user?id=1173610489\">ID 1173610489</a>" in text
    assert "📦 Тип: Групповое занятие" in text
    assert "• Группа 1: КЛАССИЧЕСКАЯ ХОРЕОГРАФИЯ (взрослые 14+)" in text
    assert "• Группа 2: РАСТЯЖКА" in text
    assert "• Цена занятия:" not in text
    assert "• Возраст:" not in text
    assert "занятий в неделю" not in text
    assert "• Кол-во занятий:" not in text
    assert "• Username:" not in text
    assert "• Написать:" not in text
    assert "📌 Статус:\nОжидается оплата" in text
    assert "waiting_payment" not in text
