"""
Seed demo data: directions, groups, teachers, students, abonements, schedule.

Usage (defaults wipe + seed):
    python scripts/seed_demo_data.py

Optional args:
    --no-wipe                : keep existing data (skip cleanup)
    --time-from 19:00        : start time for generated lessons
    --duration 60            : duration in minutes
    --week-start 2026-02-16  : ISO date (inclusive)
    --week-end 2026-02-22    : ISO date (inclusive)
    --base-price 1000        : base price for directions
    --random-seed 42         : seed for deterministic dates
"""

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.db import Session  # noqa: E402
from dance_studio.db.models import (  # noqa: E402
    Attendance,
    Direction,
    Group,
    GroupAbonement,
    GroupAbonementActionLog,
    Schedule,
    Staff,
    User,
)


FITNESS = [
    "Пилатес",
    "Карате",
    "Комбат",
    "Здоровая спина + МФР",
    "«Попа как у Ким»",
]

DANCE = [
    "Хип-хоп",
    "Джаз фанк",
    "Герли хип хоп",
    "Леди",
    "Классическая хореография",
]

TEACHER_TG_BASE = 880_000_000
STUDENT_TG_BASE = 990_000_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed demo data")
    parser.add_argument("--no-wipe", action="store_true", help="Skip cleanup")
    parser.add_argument("--time-from", default="19:00")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--time-window-start", default="10:00", help="earliest start for random slots")
    parser.add_argument("--time-window-end", default="21:00", help="latest start for random slots")
    parser.add_argument("--week-start", default="2026-02-16")
    parser.add_argument("--week-end", default="2026-02-22")
    parser.add_argument("--base-price", type=int, default=1000)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def to_time(value: str) -> dt.time:
    return dt.datetime.strptime(value, "%H:%M").time()


def daterange(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def wipe_demo(db: Session, all_titles: list[str]) -> None:
    # Directions and groups by title/name
    groups = db.query(Group).filter(Group.name.in_([f"{t} 1" for t in all_titles])).all()
    group_ids = [g.id for g in groups]

    # Delete schedules tied to groups
    if group_ids:
        db.query(Attendance).filter(Attendance.schedule_id.in_(db.query(Schedule.id).filter(Schedule.group_id.in_(group_ids)))).delete(synchronize_session=False)
        db.query(Schedule).filter(Schedule.group_id.in_(group_ids)).delete(synchronize_session=False)

    # Delete abonement logs, abonements for users we will delete or groups we target
    abonements = db.query(GroupAbonement).filter(GroupAbonement.group_id.in_(group_ids)).all()
    abon_ids = [a.id for a in abonements]
    if abon_ids:
        db.query(GroupAbonementActionLog).filter(GroupAbonementActionLog.abonement_id.in_(abon_ids)).delete(synchronize_session=False)
    db.query(GroupAbonement).filter(GroupAbonement.id.in_(abon_ids)).delete(synchronize_session=False)

    db.query(Group).filter(Group.id.in_(group_ids)).delete(synchronize_session=False)
    db.query(Direction).filter(Direction.title.in_(all_titles)).delete(synchronize_session=False)

    # Remove teachers/students in reserved telegram_id ranges and name prefix
    db.query(Staff).filter(
        (Staff.telegram_id >= TEACHER_TG_BASE) & (Staff.telegram_id < TEACHER_TG_BASE + 10_000)
    ).delete(synchronize_session=False)
    db.query(User).filter(
        (User.telegram_id >= TEACHER_TG_BASE) & (User.telegram_id < TEACHER_TG_BASE + 10_000)
        | ((User.telegram_id >= STUDENT_TG_BASE) & (User.telegram_id < STUDENT_TG_BASE + 10_000))
    ).delete(synchronize_session=False)

    db.commit()


def create_direction(db: Session, title: str, direction_type: str, base_price: int) -> Direction:
    direction = Direction(
        title=title,
        direction_type=direction_type,
        description="Демо направление",
        base_price=base_price,
        status="active",
        is_popular=0,
    )
    db.add(direction)
    db.flush()
    return direction


def create_teacher(db: Session, name: str, idx: int) -> Staff:
    tg_id = TEACHER_TG_BASE + idx
    user = User(
        telegram_id=tg_id,
        username=f"teacher_{tg_id}",
        name=name,
        status="active",
    )
    db.add(user)
    db.flush()
    staff = Staff(
        name=name,
        telegram_id=tg_id,
        position="учитель",
        status="active",
        teaches=1,
    )
    db.add(staff)
    db.flush()
    return staff


def create_students_and_abonements(db: Session, group: Group, count: int, start_idx: int, base_valid_from: dt.date) -> None:
    for i in range(count):
        tg_id = STUDENT_TG_BASE + start_idx + i
        user = User(
            telegram_id=tg_id,
            username=f"student_{tg_id}",
            name=f"Студент {group.name} {i+1}",
            status="active",
        )
        db.add(user)
        db.flush()
        abon = GroupAbonement(
            user_id=user.id,
            group_id=group.id,
            balance_credits=8,
            status="active",
            valid_from=dt.datetime.combine(base_valid_from, dt.time.min),
            valid_to=dt.datetime.combine(base_valid_from + dt.timedelta(days=60), dt.time.min),
        )
        db.add(abon)
    db.flush()


def create_group(db: Session, direction: Direction, teacher: Staff) -> Group:
    group = Group(
        direction_id=direction.direction_id,
        teacher_id=teacher.id,
        name=f"{direction.title} 1",
        description=f"Группа для направления {direction.title}",
        age_group="18+",
        duration_minutes=60,
        lessons_per_week=2,
        max_students=20,
    )
    db.add(group)
    db.flush()
    return group


def create_schedule_for_group(db: Session, group: Group, teacher: Staff, days: list[dt.date], pick_time_fn, duration_minutes: int, title: str):
    for day in days:
        time_from = pick_time_fn(day)
        if not time_from:
            continue
        time_to = (dt.datetime.combine(dt.date.today(), time_from) + dt.timedelta(minutes=duration_minutes)).time()
        sched = Schedule(
            object_type="group",
            object_id=group.id,
            group_id=group.id,
            teacher_id=teacher.id,
            date=day,
            time_from=time_from,
            time_to=time_to,
            status="scheduled",
            title=title,
        )
        db.add(sched)
    db.flush()


def pick_two_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    days = list(daterange(start, end))
    if len(days) <= 2:
        return days
    return random.sample(days, 2)


def build_time_slots(start: dt.time, end: dt.time, duration_minutes: int) -> list[dt.time]:
    slots = []
    cur_dt = dt.datetime.combine(dt.date.today(), start)
    end_dt = dt.datetime.combine(dt.date.today(), end)
    while cur_dt <= end_dt:
        slots.append(cur_dt.time())
        cur_dt += dt.timedelta(minutes=duration_minutes)
    return slots


def pick_slot_without_conflict(date: dt.date, slots: list[dt.time], duration_minutes: int, occupied: dict) -> dt.time | None:
    random.shuffle(slots)
    for start in slots:
        end_time = (dt.datetime.combine(date, start) + dt.timedelta(minutes=duration_minutes)).time()
        conflict = False
        for (s, e) in occupied.get(date, []):
            if not (end_time <= s or e <= start):
                conflict = True
                break
        if not conflict:
            occupied.setdefault(date, []).append((start, end_time))
            return start
    return None


def main():
    args = parse_args()
    random.seed(args.random_seed)

    week_start = dt.datetime.strptime(args.week_start, "%Y-%m-%d").date()
    week_end = dt.datetime.strptime(args.week_end, "%Y-%m-%d").date()
    window_start = to_time(args.time_window_start)
    window_end = to_time(args.time_window_end)
    slots = build_time_slots(window_start, window_end, args.duration)
    occupied: dict[dt.date, list[tuple[dt.time, dt.time]]] = {}

    all_titles = FITNESS + DANCE
    db = Session()
    try:
        if not args.no_wipe:
            wipe_demo(db, all_titles)

        teacher_idx = 0
        student_idx = 0
        for title in FITNESS:
            direction = create_direction(db, title, "sport", args.base_price)
            teacher = create_teacher(db, f"[y] {title} — преподаватель", teacher_idx)
            teacher_idx += 1
            group = create_group(db, direction, teacher)
            create_students_and_abonements(db, group, 5, student_idx, week_start)
            student_idx += 5
            dates = pick_two_dates(week_start, week_end)
            pick_fn = lambda day, s=slots: pick_slot_without_conflict(day, s.copy(), args.duration, occupied)
            create_schedule_for_group(db, group, teacher, dates, pick_fn, args.duration, title)

        for title in DANCE:
            direction = create_direction(db, title, "dance", args.base_price)
            teacher = create_teacher(db, f"[y] {title} — преподаватель", teacher_idx)
            teacher_idx += 1
            group = create_group(db, direction, teacher)
            create_students_and_abonements(db, group, 5, student_idx, week_start)
            student_idx += 5
            dates = pick_two_dates(week_start, week_end)
            pick_fn = lambda day, s=slots: pick_slot_without_conflict(day, s.copy(), args.duration, occupied)
            create_schedule_for_group(db, group, teacher, dates, pick_fn, args.duration, title)

        db.commit()
        print("✅ Demo data seeded successfully")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
