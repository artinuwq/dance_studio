from __future__ import annotations

import random
from datetime import date, datetime, timedelta, time
from types import SimpleNamespace

import pytest

from dance_studio.core import abonement_notifications

from dance_studio.auth.services.common import normalize_phone_e164
from dance_studio.web.routes import bookings as bookings_routes
from dance_studio.web.routes import admin as admin_routes
from dance_studio.web.constants import ATTENDANCE_INTENTION_LOCKED_MESSAGE
from dance_studio.web.services.admin import (
    _append_merge_note,
    _has_slot_conflict,
    _minutes_to_time_str,
    _parse_iso_date,
    _parse_month_start,
    _schedule_group_id,
    _subtract_busy_intervals,
    _time_to_minutes,
)
from dance_studio.web.services.payments import (
    PAYMENT_PROFILE_PRIMARY_SLOT,
    _select_payment_slot_for_context,
)
from dance_studio.web.services.attendance import (
    _attendance_intention_lock_info,
    _attendance_marking_window_info,
    _schedule_start_datetime,
    _serialize_attendance_intention_with_lock,
)
from dance_studio.web.services.studio_rules import (
    PRIMARY_OWNER_KEY,
    SECONDARY_OWNER_KEY,
    interval_overlaps_service_break,
    owner_for_group_direction,
    owner_for_interval,
)


def _schedule(*, date=None, time_from=None, start_time=None, group_id=None, object_type=None, object_id=None):
    return SimpleNamespace(
        date=date,
        time_from=time_from,
        start_time=start_time,
        group_id=group_id,
        object_type=object_type,
        object_id=object_id,
    )


def _bruteforce_subtract(start: int, end: int, busy_intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if end <= start:
        return []

    free = [True] * (end - start)
    for busy_start, busy_end in busy_intervals:
        lo = max(start, busy_start)
        hi = min(end, busy_end)
        if hi <= lo:
            continue
        for idx in range(lo - start, hi - start):
            free[idx] = False

    segments: list[tuple[int, int]] = []
    seg_start = None
    for idx, is_free in enumerate(free):
        minute = start + idx
        if is_free and seg_start is None:
            seg_start = minute
        if not is_free and seg_start is not None:
            segments.append((seg_start, minute))
            seg_start = None
    if seg_start is not None:
        segments.append((seg_start, end))
    return segments


def _bruteforce_conflict(start_min: int, duration_minutes: int, busy_intervals: list[tuple[int, int]]) -> bool:
    end_min = start_min + duration_minutes
    for minute in range(start_min, end_min):
        for busy_start, busy_end in busy_intervals:
            if busy_start <= minute < busy_end:
                return True
    return False


def test_admin_time_helpers():
    assert _time_to_minutes(time(9, 30)) == 570
    assert _minutes_to_time_str(0) == "00:00"
    assert _minutes_to_time_str(615) == "10:15"


def test_admin_busy_interval_subtraction():
    segments = _subtract_busy_intervals(540, 720, [(570, 600), (630, 660)])
    assert segments == [(540, 570), (600, 630), (660, 720)]

    # Busy intervals outside range should be ignored safely.
    segments2 = _subtract_busy_intervals(540, 600, [(0, 500), (610, 700)])
    assert segments2 == [(540, 600)]


def test_admin_slot_conflict():
    busy = [(600, 630), (700, 730)]

    assert _has_slot_conflict(590, 20, busy) is True
    assert _has_slot_conflict(630, 30, busy) is False
    assert _has_slot_conflict(705, 10, busy) is True


def test_admin_time_helpers_roundtrip_every_minute():
    for minute in range(24 * 60):
        hhmm = _minutes_to_time_str(minute)
        hh, mm = [int(part) for part in hhmm.split(":")]
        assert _time_to_minutes(time(hh, mm)) == minute


def test_admin_busy_interval_subtraction_boundary_cases():
    assert _subtract_busy_intervals(600, 660, [(540, 600)]) == [(600, 660)]
    assert _subtract_busy_intervals(600, 660, [(660, 700)]) == [(600, 660)]
    assert _subtract_busy_intervals(600, 660, [(600, 660)]) == []
    assert _subtract_busy_intervals(600, 660, [(590, 670)]) == []


def test_group_delete_blockers_formatter_returns_none_without_dependencies():
    assert bookings_routes._format_group_delete_blockers(
        {"schedules": 0, "booking_requests": 0, "abonements": 0}
    ) is None


def test_group_delete_blockers_formatter_lists_all_dependency_types():
    message = bookings_routes._format_group_delete_blockers(
        {"schedules": 2, "booking_requests": 3, "abonements": 1}
    )

    assert message is not None
    assert "расписании (2)" in message
    assert "заявках (3)" in message
    assert "абонементах (1)" in message


def test_sync_rental_with_schedule_reactivates_cancelled_rental_on_move():
    rental = SimpleNamespace(
        date=date(2026, 3, 8),
        time_from=time(10, 0),
        time_to=time(11, 0),
        start_time=None,
        end_time=None,
        duration_minutes=60,
        status="CANCELLED",
        activity_status="cancelled",
    )

    admin_routes._sync_rental_with_schedule(
        rental,
        target_date=date(2026, 3, 9),
        target_time_from=time(12, 0),
        target_time_to=time(13, 0),
        status="scheduled",
    )

    assert rental.status == "scheduled"
    assert rental.activity_status == "active"


def test_admin_busy_interval_subtraction_property_randomized():
    rnd = random.Random(20260304)
    day_end = 24 * 60

    for _ in range(300):
        start = rnd.randint(0, day_end)
        end = rnd.randint(start, day_end)
        busy: list[tuple[int, int]] = []
        for __ in range(rnd.randint(0, 10)):
            a = rnd.randint(0, day_end)
            b = rnd.randint(0, day_end)
            busy_start, busy_end = sorted((a, b))
            busy.append((busy_start, busy_end))
        busy.sort(key=lambda item: (item[0], item[1]))

        expected = _bruteforce_subtract(start, end, busy)
        assert _subtract_busy_intervals(start, end, busy) == expected


def test_admin_slot_conflict_property_randomized_and_boundaries():
    # Touching boundaries should not conflict for half-open intervals [start, end).
    assert _has_slot_conflict(600, 30, [(630, 700)]) is False
    assert _has_slot_conflict(600, 30, [(570, 600)]) is False
    assert _has_slot_conflict(600, 30, [(629, 700)]) is True

    rnd = random.Random(20260304)
    day_end = 24 * 60

    for _ in range(300):
        start_min = rnd.randint(0, day_end)
        duration = rnd.randint(1, 180)
        busy: list[tuple[int, int]] = []
        for __ in range(rnd.randint(0, 8)):
            a = rnd.randint(0, day_end)
            b = rnd.randint(0, day_end)
            busy_start, busy_end = sorted((a, b))
            busy.append((busy_start, busy_end))

        expected = _bruteforce_conflict(start_min, duration, busy)
        assert _has_slot_conflict(start_min, duration, busy) is expected


def test_admin_parse_iso_date():
    parsed = _parse_iso_date("2026-03-04", "date_from")
    assert parsed.isoformat() == "2026-03-04"

    with pytest.raises(ValueError):
        _parse_iso_date("", "date_from")
    with pytest.raises(ValueError):
        _parse_iso_date("04-03-2026", "date_from")


def test_admin_parse_month_start():
    parsed = _parse_month_start("2026-03")
    assert parsed.isoformat() == "2026-03-01"

    now = datetime.now()
    default_month = _parse_month_start(None)
    assert default_month.isoformat() == datetime(now.year, now.month, 1).date().isoformat()

    with pytest.raises(ValueError):
        _parse_month_start("2026/03")


def test_admin_parse_stats_date_range():
    assert admin_routes._parse_stats_date_range("2026-03-01", "2026-03-31") == (
        date(2026, 3, 1),
        date(2026, 3, 31),
    )
    assert admin_routes._parse_stats_date_range(None, None) == (None, None)

    with pytest.raises(ValueError):
        admin_routes._parse_stats_date_range("2026-03-31", "2026-03-01")

    with pytest.raises(ValueError):
        admin_routes._parse_stats_date_range("bad-date", "2026-03-01")


def test_admin_booking_expected_amount_prefers_requested_amount():
    booking = SimpleNamespace(
        requested_amount="2750",
        object_type="individual",
        duration_minutes=60,
        time_from=None,
        time_to=None,
        lessons_count=None,
        group_id=None,
    )

    assert admin_routes._booking_expected_amount_rub(object(), booking) == 2750


def test_admin_booking_expected_amount_falls_back_to_non_group_rate(monkeypatch):
    monkeypatch.setattr(
        admin_routes,
        "compute_non_group_booking_base_amount",
        lambda db, *, object_type, duration_minutes: 3600 if object_type == "individual" and duration_minutes == 90 else None,
    )
    booking = SimpleNamespace(
        requested_amount=None,
        object_type="individual",
        duration_minutes=90,
        time_from=None,
        time_to=None,
        lessons_count=None,
        group_id=None,
    )

    assert admin_routes._booking_expected_amount_rub(object(), booking) == 3600


def test_admin_schedule_group_id_and_merge_note():
    assert _schedule_group_id(_schedule(group_id=5, object_type="group", object_id=7)) == 5
    assert _schedule_group_id(_schedule(group_id=None, object_type="group", object_id=7)) == 7
    assert _schedule_group_id(_schedule(group_id=None, object_type="individual", object_id=7)) is None

    assert _append_merge_note(None, "note") == "note"
    assert _append_merge_note("base", "note") == "base\n\nnote"
    assert _append_merge_note("base\n\nnote", "note") == "base\n\nnote"


def test_attendance_schedule_start_datetime_prefers_time_from():
    day = datetime(2026, 3, 4).date()
    s = _schedule(date=day, time_from=time(10, 30), start_time=time(9, 0))
    start_dt = _schedule_start_datetime(s)

    assert start_dt is not None
    assert start_dt.isoformat() == "2026-03-04T10:30:00"


def test_attendance_lock_info_unknown_schedule_time():
    s = _schedule(date=None, time_from=None, start_time=None)
    lock = _attendance_intention_lock_info(s)

    assert lock["is_locked"] is False
    assert lock["cutoff_at"] is None
    assert lock["starts_at"] is None
    assert lock["lock_message"] is None


def test_attendance_lock_info_locked_and_unlocked_cases():
    now = datetime.now()

    # Start far enough in future, lock should be open.
    unlocked_start = now + timedelta(hours=4)
    s_unlocked = _schedule(date=unlocked_start.date(), time_from=unlocked_start.time(), start_time=None)
    unlocked = _attendance_intention_lock_info(s_unlocked)
    assert unlocked["is_locked"] is False

    # Start soon, lock should already be closed.
    locked_start = now + timedelta(minutes=30)
    s_locked = _schedule(date=locked_start.date(), time_from=locked_start.time(), start_time=None)
    locked = _attendance_intention_lock_info(s_locked)
    assert locked["is_locked"] is True
    assert locked["lock_message"] == ATTENDANCE_INTENTION_LOCKED_MESSAGE


def test_attendance_marking_window_phases():
    now = datetime.now()

    unknown = _attendance_marking_window_info(_schedule(date=None, time_from=None, start_time=None))
    assert unknown["phase"] == "unknown"
    assert unknown["is_open"] is False

    before = now + timedelta(days=1)
    before_info = _attendance_marking_window_info(_schedule(date=before.date(), time_from=before.time(), start_time=None))
    assert before_info["phase"] == "before_start"
    assert before_info["is_open"] is False

    open_start = now.replace(second=0, microsecond=0)
    open_info = _attendance_marking_window_info(_schedule(date=open_start.date(), time_from=open_start.time(), start_time=None))
    assert open_info["phase"] == "marking_open"
    assert open_info["is_open"] is True

    closed = now - timedelta(days=1)
    closed_info = _attendance_marking_window_info(_schedule(date=closed.date(), time_from=closed.time(), start_time=None))
    assert closed_info["phase"] == "marking_closed"
    assert closed_info["is_open"] is False


def test_serialize_attendance_intention_with_lock_adds_banner():
    row = SimpleNamespace(
        status="will_miss",
        reason="ill",
        updated_at=datetime(2026, 3, 4, 12, 0, 0),
        created_at=datetime(2026, 3, 4, 11, 0, 0),
    )
    lock_info = {
        "is_locked": True,
        "cutoff_at": "2026-03-04T09:30:00",
        "starts_at": "2026-03-04T12:00:00",
        "lock_message": ATTENDANCE_INTENTION_LOCKED_MESSAGE,
    }

    payload = _serialize_attendance_intention_with_lock(row, lock_info)
    assert payload["has_intention"] is True
    assert payload["status"] == "will_miss"
    assert payload["banner"] == ATTENDANCE_INTENTION_LOCKED_MESSAGE


def test_schedule_route_time_bounds_and_slot_label_helpers():
    slot = SimpleNamespace(
        date=date(2026, 3, 5),
        time_from=time(19, 0),
        time_to=time(20, 0),
        start_time=time(18, 30),
        end_time=time(20, 30),
    )
    assert admin_routes._schedule_time_bounds(slot) == (time(19, 0), time(20, 0))
    slot_label = admin_routes._format_schedule_slot_label(slot)
    assert slot_label.startswith("05.03.2026 ")
    assert "19:00" in slot_label
    assert "20:00" in slot_label

    fallback_slot = SimpleNamespace(
        date=date(2026, 3, 5),
        time_from=None,
        time_to=None,
        start_time=time(18, 30),
        end_time=time(20, 30),
    )
    assert admin_routes._schedule_time_bounds(fallback_slot) == (time(18, 30), time(20, 30))
    fallback_label = admin_routes._format_schedule_slot_label(fallback_slot)
    assert fallback_label.startswith("05.03.2026 ")
    assert "18:30" in fallback_label
    assert "20:30" in fallback_label


def test_schedule_route_send_group_chat_message_short_circuits_without_config(monkeypatch):
    monkeypatch.setattr(admin_routes, "BOT_TOKEN", None)

    ok_chat_missing, err_chat_missing = admin_routes._send_group_chat_message(None, "msg")
    assert ok_chat_missing is False
    assert err_chat_missing == "group_chat_not_configured"

    ok_token_missing, err_token_missing = admin_routes._send_group_chat_message(123, "msg")
    assert ok_token_missing is False
    assert err_token_missing == "bot_token_not_set"


def test_schedule_route_send_group_chat_message_success_and_error(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, ok: bool, text: str = ""):
            self.ok = ok
            self.text = text

    def _post_ok(url, json, timeout):
        calls.append((url, json, timeout))
        return _Resp(True)

    monkeypatch.setattr(admin_routes, "BOT_TOKEN", "token123")
    monkeypatch.setattr(admin_routes.requests, "post", _post_ok)

    ok, err = admin_routes._send_group_chat_message(987654321, "hello")
    assert ok is True
    assert err is None
    assert len(calls) == 1
    assert calls[0][0].endswith("/bottoken123/sendMessage")
    assert calls[0][1]["chat_id"] == 987654321
    assert calls[0][1]["text"] == "hello"
    assert calls[0][2] == 10

    def _post_fail(url, json, timeout):
        return _Resp(False, "TG_ERROR")

    monkeypatch.setattr(admin_routes.requests, "post", _post_fail)
    ok_fail, err_fail = admin_routes._send_group_chat_message(987654321, "hello")
    assert ok_fail is False
    assert err_fail == "telegram_send_failed"


def test_schedule_route_group_conflict_helper_detects_overlap():
    class _FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self._rows

    class _FakeDb:
        def __init__(self, rows):
            self._rows = rows

        def query(self, *args, **kwargs):
            return _FakeQuery(self._rows)

    day = date(2026, 3, 5)
    rows = [
        SimpleNamespace(time_from=time(9, 0), time_to=time(10, 0), start_time=None, end_time=None),
        SimpleNamespace(time_from=time(11, 0), time_to=time(12, 0), start_time=None, end_time=None),
        SimpleNamespace(time_from=None, time_to=None, start_time=None, end_time=None),
    ]
    fake_db = _FakeDb(rows)

    no_conflict = admin_routes._has_group_schedule_conflict(
        fake_db,
        schedule_id=1,
        group_id=10,
        target_date=day,
        target_time_from=time(10, 0),
        target_time_to=time(11, 0),
    )
    assert no_conflict is False

    has_conflict = admin_routes._has_group_schedule_conflict(
        fake_db,
        schedule_id=1,
        group_id=10,
        target_date=day,
        target_time_from=time(10, 30),
        target_time_to=time(11, 30),
    )
    assert has_conflict is True


def test_studio_service_break_overlap_boundaries():
    assert interval_overlaps_service_break(time(14, 29), time(14, 30)) is False
    assert interval_overlaps_service_break(time(15, 0), time(15, 30)) is False
    assert interval_overlaps_service_break(time(14, 30), time(15, 0)) is True
    assert interval_overlaps_service_break(time(14, 0), time(15, 0)) is True
    assert interval_overlaps_service_break(time(14, 45), time(15, 30)) is True


def test_studio_owner_resolution_for_rental_and_individual():
    monday = date(2026, 3, 2)  # Monday
    tuesday = date(2026, 3, 3)  # Tuesday

    # Mon morning -> secondary owner, Mon evening -> primary owner.
    assert owner_for_interval(monday, time(9, 30), time(10, 30)) == SECONDARY_OWNER_KEY
    assert owner_for_interval(monday, time(15, 0), time(16, 0)) == PRIMARY_OWNER_KEY

    # Tue morning -> primary owner, Tue evening -> secondary owner.
    assert owner_for_interval(tuesday, time(9, 30), time(10, 30)) == PRIMARY_OWNER_KEY
    assert owner_for_interval(tuesday, time(15, 0), time(16, 0)) == SECONDARY_OWNER_KEY

    # Any overlap with service break is invalid/undefined owner.
    assert owner_for_interval(monday, time(14, 20), time(14, 40)) is None
    assert owner_for_interval(monday, time(14, 30), time(15, 0)) is None


def test_studio_owner_resolution_for_group_direction():
    assert owner_for_group_direction("sport") == PRIMARY_OWNER_KEY
    assert owner_for_group_direction("dance") == SECONDARY_OWNER_KEY
    assert owner_for_group_direction("  DANCE  ") == SECONDARY_OWNER_KEY
    assert owner_for_group_direction("unknown") is None


def test_payment_slot_selection_for_context():
    secondary_active = 3

    # Group mapping by direction.
    assert (
        _select_payment_slot_for_context(
            object_type="group",
            group_direction_type="sport",
            secondary_owner_active_slot=secondary_active,
        )
        == PAYMENT_PROFILE_PRIMARY_SLOT
    )
    assert (
        _select_payment_slot_for_context(
            object_type="group",
            group_direction_type="dance",
            secondary_owner_active_slot=secondary_active,
        )
        == 3
    )

    # Rental/individual mapping by weekday segment.
    monday = date(2026, 3, 2)  # Monday
    assert (
        _select_payment_slot_for_context(
            object_type="rental",
            booking_date=monday,
            time_from=time(10, 0),
            time_to=time(11, 0),
            secondary_owner_active_slot=secondary_active,
        )
        == 3
    )
    assert (
        _select_payment_slot_for_context(
            object_type="individual",
            booking_date=monday,
            time_from=time(16, 0),
            time_to=time(17, 0),
            secondary_owner_active_slot=secondary_active,
        )
        == PAYMENT_PROFILE_PRIMARY_SLOT
    )


def test_abonement_notification_resolve_group_ids_for_booking_deduplicates_and_keeps_main_group():
    booking = SimpleNamespace(
        group_id=12,
        bundle_group_ids_json="[14, 12, 14, 18, 0, \"bad\"]",
    )

    assert abonement_notifications.resolve_group_ids_for_booking(booking) == [12, 14, 18]


def test_abonement_notification_dispatch_ref_prefers_bundle_id():
    bundled = SimpleNamespace(id=10, bundle_id="bundle-123")
    single = SimpleNamespace(id=11, bundle_id=None)

    assert abonement_notifications.build_abonement_dispatch_ref(bundled) == "bundle:bundle-123"
    assert abonement_notifications.build_abonement_dispatch_ref(single) == "abonement:11"


def test_abonement_group_access_message_lists_links_and_missing_link_note():
    message = abonement_notifications.build_group_access_message(
        [
            {
                "group_name": "Hip-Hop Teens",
                "chat_invite_link": "https://t.me/+abc123",
                "next_session_date": date(2026, 3, 10),
            },
            {
                "group_name": "Ballet Mini",
                "chat_invite_link": None,
                "next_session_date": date(2026, 3, 11),
            },
        ]
    )

    assert message is not None
    assert "Hip-Hop Teens" in message
    assert "https://t.me/+abc123" in message
    assert "Ballet Mini" in message
    assert "ссылка пока не настроена" in message


def test_abonement_one_left_notice_rule_only_matches_single_group_multi_with_one_credit():
    assert abonement_notifications.is_one_left_group_abonement_notice_due(
        SimpleNamespace(status="active", abonement_type="multi", bundle_size=1, balance_credits=1)
    ) is True
    assert abonement_notifications.is_one_left_group_abonement_notice_due(
        SimpleNamespace(status="active", abonement_type="single", bundle_size=1, balance_credits=1)
    ) is False
    assert abonement_notifications.is_one_left_group_abonement_notice_due(
        SimpleNamespace(status="active", abonement_type="multi", bundle_size=2, balance_credits=1)
    ) is False
    assert abonement_notifications.is_one_left_group_abonement_notice_due(
        SimpleNamespace(status="active", abonement_type="multi", bundle_size=1, balance_credits=2)
    ) is False


def test_abonement_bundle_expiry_notice_rule_matches_two_and_three_group_bundles_in_7_day_window():
    now = datetime(2026, 3, 8, 10, 0, 0)

    assert abonement_notifications.is_bundle_expiry_notice_due(
        SimpleNamespace(status="active", bundle_size=2, valid_to=datetime(2026, 3, 12, 23, 59, 59)),
        now=now,
    ) is True
    assert abonement_notifications.is_bundle_expiry_notice_due(
        SimpleNamespace(status="active", bundle_size=3, valid_to=datetime(2026, 3, 15, 23, 59, 59)),
        now=now,
    ) is True
    assert abonement_notifications.is_bundle_expiry_notice_due(
        SimpleNamespace(status="active", bundle_size=2, valid_to=datetime(2026, 3, 20, 23, 59, 59)),
        now=now,
    ) is False
    assert abonement_notifications.is_bundle_expiry_notice_due(
        SimpleNamespace(status="inactive", bundle_size=2, valid_to=datetime(2026, 3, 12, 23, 59, 59)),
        now=now,
    ) is False


def test_admin_discount_payload_validation_rules():
    payload = {
        "discount_type": "percentage",
        "value": 15,
        "is_one_time": True,
        "comment": "promo",
    }
    assert admin_routes._validate_discount_payload(payload) == ("percentage", 15, True, "promo")

    with pytest.raises(ValueError, match="1..100"):
        admin_routes._validate_discount_payload({"discount_type": "percentage", "value": 101})

    with pytest.raises(ValueError, match=">= 1"):
        admin_routes._validate_discount_payload({"discount_type": "fixed", "value": 0})


def test_staff_role_hierarchy_edit_rules():
    assert admin_routes._can_edit_staff_by_roles("тех. админ", "владелец") is True
    assert admin_routes._can_edit_staff_by_roles("владелец", "старший админ") is True
    assert admin_routes._can_edit_staff_by_roles("старший админ", "владелец") is False
    assert admin_routes._can_edit_staff_by_roles("владелец", "тех. админ") is False


def test_staff_role_hierarchy_assignment_rules():
    assert admin_routes._can_assign_staff_role_by_roles("тех. админ", "владелец") is True
    assert admin_routes._can_assign_staff_role_by_roles("владелец", "старший админ") is True
    assert admin_routes._can_assign_staff_role_by_roles("старший админ", "владелец") is False
    assert admin_routes._can_assign_staff_role_by_roles("владелец", "тех. админ") is False


def test_normalize_phone_e164_handles_russian_formats():
    assert normalize_phone_e164("8 (999) 123-45-67") == "+79991234567"
    assert normalize_phone_e164("9991234567") == "+79991234567"
    assert normalize_phone_e164("+7 999 123 45 67") == "+79991234567"
    assert normalize_phone_e164("abc") is None
