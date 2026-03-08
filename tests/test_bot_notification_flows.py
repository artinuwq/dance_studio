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
