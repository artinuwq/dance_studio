"""
Utility script to seed the database with fake users.

Usage:
    python scripts/create_fake_users.py --count 20 --start-telegram 900000000

Requires environment variables from .env (DATABASE_URL, etc.) just like the app.
"""

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.db import Session  # noqa: E402
from dance_studio.db.models import User  # noqa: E402


NAMES = [
    "Алексей", "Мария", "Дмитрий", "Ольга", "Иван", "Анна", "Сергей", "Екатерина",
    "Никита", "Юлия", "Павел", "Наталья", "Виктор", "Ксения", "Михаил", "София",
    "Артем", "Дарья", "Роман", "Алиса", "Игорь", "Татьяна", "Кирилл", "Вероника",
    "Егор", "Полина", "Максим", "Виктория", "Степан", "Людмила",
]


def generate_users(count: int, start_telegram: int, phone_prefix: str = "+7999") -> list[User]:
    """Create unsaved User objects with unique telegram_id."""
    users: list[User] = []
    used_tg = set()
    for i in range(count):
        tg_id = start_telegram + i
        used_tg.add(tg_id)
        name = random.choice(NAMES) + f" {random.randint(1, 99)}"
        username = f"fake_user_{tg_id}"
        phone = f"{phone_prefix}{tg_id % 10_000:04d}"
        users.append(
            User(
                telegram_id=tg_id,
                username=username,
                phone=phone,
                name=name,
                status="active",
            )
        )
    return users


def main():
    parser = argparse.ArgumentParser(description="Seed fake users into database.")
    parser.add_argument("--count", type=int, default=20, help="How many users to create")
    parser.add_argument(
        "--start-telegram",
        type=int,
        default=900_000_000,
        help="Starting telegram_id (increments by 1 for each user)",
    )
    args = parser.parse_args()

    db = Session()
    try:
        new_users = generate_users(args.count, args.start_telegram)
        # Filter out telegram_ids that already exist
        existing_ids = {
            row[0]
            for row in db.query(User.telegram_id)
            .filter(User.telegram_id.in_([u.telegram_id for u in new_users]))
            .all()
        }
        to_insert = [u for u in new_users if u.telegram_id not in existing_ids]
        if not to_insert:
            print("Нет новых пользователей для вставки (все telegram_id уже существуют).")
            return

        db.add_all(to_insert)
        db.commit()
        print(f"Создано {len(to_insert)} пользователей (пропущено существующих: {len(existing_ids)})")
        for u in to_insert:
            print(f"- {u.name} | tg_id={u.telegram_id} | @{u.username} | {u.phone}")
    except Exception as exc:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
