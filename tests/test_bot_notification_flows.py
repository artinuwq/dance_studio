from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_FILE = ROOT / "src" / "dance_studio" / "bot" / "bot.py"


def _window(source: str, marker: str, size: int = 2200) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_attendance_reminder_markup_has_will_attend_and_will_miss_buttons():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "def _reminder_markup(schedule_id: int) -> InlineKeyboardMarkup:", size=700)

    assert 'callback_data=f"attcome:{schedule_id}"' in window
    assert 'callback_data=f"attmiss:{schedule_id}"' in window


def test_vk_attendance_reminder_keyboard_has_callback_buttons():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "def _vk_reminder_keyboard(schedule_id: int) -> dict:", size=1500)

    assert '"command": ATTENDANCE_REMINDER_COMMAND' in window
    assert '"type": "callback"' in window
    assert '_payload(ATTENDANCE_WILL_ATTEND_STATUS)' in window
    assert '_payload(ATTENDANCE_WILL_MISS_STATUS)' in window


def test_attendance_reminder_delivery_resolves_interactive_channels_and_sends_vk_keyboard():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def _send_attendance_reminder_to_user(db, schedule: Schedule, user: User) -> None:", size=3200)

    assert "channels = _resolve_attendance_reminder_channels(db, user.id)" in window
    assert "vk_provider = VkNotificationProvider()" in window
    assert '{"keyboard": _vk_reminder_keyboard(schedule.id)}' in window
    assert "row.vk_peer_id = vk_peer_id" in window
    assert "row.vk_message_id = vk_message_id" in window


def test_locked_attendance_reminders_edit_vk_messages_and_remove_keyboard():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def close_locked_attendance_reminders() -> None:", size=2200)

    assert "edit_vk_message" in window
    assert "_reminder_closed_message_text(schedule)" in window
    assert "_vk_remove_keyboard_payload()" in window


def test_reminder_background_loop_runs_teacher_summaries_and_abonement_notifications():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def process_attendance_reminders() -> None:", size=500)

    assert "await send_due_teacher_attendance_summaries()" in window
    assert "await send_due_abonement_notifications()" in window


def test_booking_callback_triggers_group_access_notification_after_activation():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def handle_booking_action(callback: CallbackQuery):", size=12000)

    assert "activated_abonement = _activate_group_abonement_from_booking(db, booking)" in window
    assert "asyncio.create_task(_notify_group_access_after_booking(booking.id, activated_abonement.id))" in window


def test_contact_share_normalizes_phone_and_flushes_before_merge():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def handle_contact_share(message):", size=2400)

    assert "normalized_phone_number = normalize_phone_e164" in window
    assert "db.flush()" in window
    assert "phone=normalized_phone_number" in window
