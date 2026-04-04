from __future__ import annotations

import logging
from collections.abc import Iterable

from dance_studio.db.models import Attendance, Group
from dance_studio.notifications.services.notification_service import NotificationService

_logger = logging.getLogger(__name__)


def _unique_positive_ints(raw_values: Iterable[object] | None) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for raw_value in raw_values or []:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def collect_group_notification_recipient_ids(
    *,
    group: Group | None,
    attendance_rows: Iterable[Attendance] | None = None,
    abonement_user_ids: Iterable[object] | None = None,
    include_teacher: bool = True,
) -> list[int]:
    user_ids: list[object] = []

    if include_teacher and group:
        teacher = getattr(group, "teacher", None)
        teacher_user_id = getattr(teacher, "user_id", None)
        if teacher_user_id:
            user_ids.append(teacher_user_id)

    for attendance in attendance_rows or []:
        user_ids.append(getattr(attendance, "user_id", None))

    user_ids.extend(abonement_user_ids or [])
    return _unique_positive_ints(user_ids)


def send_group_notifications(
    db,
    *,
    recipient_user_ids: Iterable[object],
    event_type: str,
    title: str,
    body: str,
    payload: dict | None = None,
) -> dict:
    recipients = _unique_positive_ints(recipient_user_ids)
    payload_data = {"parse_mode": "HTML"}
    if payload:
        payload_data.update(payload)

    sent_count = 0
    failed_user_ids: list[int] = []
    service = NotificationService()

    for user_id in recipients:
        try:
            notification = service.send(
                db,
                user_id=user_id,
                event_type=event_type,
                title=title,
                body=body,
                payload=payload_data,
            )
        except Exception:
            _logger.exception("Failed to send group notification to user %s", user_id)
            failed_user_ids.append(user_id)
            continue

        if str(getattr(notification, "status", "") or "").strip().lower() == "sent":
            sent_count += 1
        else:
            failed_user_ids.append(user_id)

    error = None
    if recipients and sent_count < len(recipients):
        error = "group_notification_delivery_failed" if sent_count == 0 else "group_notification_delivery_partial"

    return {
        "ok": bool(recipients) and sent_count == len(recipients),
        "recipient_count": len(recipients),
        "sent_count": sent_count,
        "failed_user_ids": failed_user_ids,
        "error": error,
    }


def notify_group_participants(
    db,
    *,
    group: Group | None,
    event_type: str,
    title: str,
    body: str,
    payload: dict | None = None,
    attendance_rows: Iterable[Attendance] | None = None,
    abonement_user_ids: Iterable[object] | None = None,
    include_teacher: bool = True,
) -> dict:
    recipient_user_ids = collect_group_notification_recipient_ids(
        group=group,
        attendance_rows=attendance_rows,
        abonement_user_ids=abonement_user_ids,
        include_teacher=include_teacher,
    )
    return send_group_notifications(
        db,
        recipient_user_ids=recipient_user_ids,
        event_type=event_type,
        title=title,
        body=body,
        payload=payload,
    )
