from __future__ import annotations

import html
from datetime import datetime, timedelta

from dance_studio.core.abonement_pricing import parse_booking_bundle_group_ids, get_next_group_date
from dance_studio.db.models import BookingRequest, Group, GroupAbonement


def _unique_group_ids(raw_values) -> list[int]:
    seen: set[int] = set()
    group_ids: list[int] = []
    for raw_value in raw_values or []:
        try:
            group_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if group_id <= 0 or group_id in seen:
            continue
        seen.add(group_id)
        group_ids.append(group_id)
    return group_ids


def resolve_group_ids_for_booking(booking: BookingRequest) -> list[int]:
    group_ids = _unique_group_ids(parse_booking_bundle_group_ids(booking))
    main_group_id = getattr(booking, "group_id", None)
    try:
        main_group_id = int(main_group_id)
    except (TypeError, ValueError):
        main_group_id = None
    if main_group_id and main_group_id in group_ids:
        group_ids = [main_group_id, *[group_id for group_id in group_ids if group_id != main_group_id]]
    return group_ids


def resolve_group_ids_for_abonement(db, abonement: GroupAbonement) -> list[int]:
    bundle_id = str(getattr(abonement, "bundle_id", "") or "").strip()
    if bundle_id:
        rows = (
            db.query(GroupAbonement)
            .filter(
                GroupAbonement.user_id == abonement.user_id,
                GroupAbonement.bundle_id == bundle_id,
            )
            .order_by(GroupAbonement.group_id.asc(), GroupAbonement.id.asc())
            .all()
        )
        return _unique_group_ids(row.group_id for row in rows)
    return _unique_group_ids([getattr(abonement, "group_id", None)])


def build_abonement_dispatch_ref(abonement: GroupAbonement) -> str:
    bundle_id = str(getattr(abonement, "bundle_id", "") or "").strip()
    if bundle_id:
        return f"bundle:{bundle_id}"
    return f"abonement:{int(abonement.id)}"


def collect_group_access_items(db, group_ids: list[int]) -> list[dict]:
    items: list[dict] = []
    for group_id in _unique_group_ids(group_ids):
        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            continue
        items.append(
            {
                "group_id": group.id,
                "group_name": group.name or f"Группа #{group.id}",
                "chat_invite_link": (group.chat_invite_link or "").strip() or None,
                "next_session_date": get_next_group_date(db, group.id),
            }
        )
    return items


def build_group_access_message(items: list[dict]) -> str | None:
    if not items:
        return None

    lines = [
        "<b>Доступ к занятиям подтвержден.</b>",
        "",
        "Информация по вашим группам:",
    ]
    for item in items:
        group_name = html.escape(str(item.get("group_name") or "Группа"))
        lines.append(f"• <b>{group_name}</b>")
        next_session_date = item.get("next_session_date")
        if next_session_date:
            lines.append(f"  Ближайшее занятие: {next_session_date.strftime('%d.%m.%Y')}")
        invite_link = (item.get("chat_invite_link") or "").strip()
        if invite_link:
            lines.append(f"  Чат группы: {html.escape(invite_link)}")
        else:
            lines.append("  Чат группы: ссылка пока не настроена, при необходимости напишите администратору.")
    return "\n".join(lines)


def is_one_left_group_abonement_notice_due(abonement: GroupAbonement) -> bool:
    abonement_type = str(getattr(abonement, "abonement_type", "") or "").strip().lower()
    bundle_size = getattr(abonement, "bundle_size", None)
    try:
        balance_credits = int(getattr(abonement, "balance_credits", 0) or 0)
    except (TypeError, ValueError):
        return False

    return (
        str(getattr(abonement, "status", "") or "").strip().lower() == "active"
        and abonement_type == "multi"
        and (bundle_size in (None, 1))
        and balance_credits == 1
    )


def is_bundle_expiry_notice_due(
    abonement: GroupAbonement,
    *,
    now: datetime | None = None,
    days_before: int = 7,
) -> bool:
    if str(getattr(abonement, "status", "") or "").strip().lower() != "active":
        return False

    bundle_size = getattr(abonement, "bundle_size", None)
    if bundle_size not in {2, 3}:
        return False

    valid_to = getattr(abonement, "valid_to", None)
    if not isinstance(valid_to, datetime):
        return False

    current = now or datetime.now()
    current_day = current.date()
    valid_to_day = valid_to.date()
    notify_from_day = valid_to_day - timedelta(days=days_before)
    return notify_from_day <= current_day <= valid_to_day
