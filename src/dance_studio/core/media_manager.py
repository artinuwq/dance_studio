"""
Модуль для управления структурой папок приложения
"""

import os
from pathlib import Path

# Базовые пути: всё пользовательское в var/
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BASE_DIR = PROJECT_ROOT
VAR_ROOT = PROJECT_ROOT / "var"
MEDIA_DIR = VAR_ROOT / "media"
USERS_MEDIA_DIR = MEDIA_DIR / "users"
TEACHERS_MEDIA_DIR = MEDIA_DIR / "teachers"
NEWS_MEDIA_DIR = MEDIA_DIR / "news"


def create_required_directories():
    """
    Создает все необходимые папки при запуске приложения
    """
    directories = [
        MEDIA_DIR,
        USERS_MEDIA_DIR,
        TEACHERS_MEDIA_DIR,
        NEWS_MEDIA_DIR,
    ]
    
    for directory in directories:
        try:
            os.makedirs(directory, exist_ok=True)
            print(f"[media] ensured dir: {directory}")
        except Exception as e:
            print(f"❌ Ошибка при создании папки {directory}: {e}")


def get_user_media_dir(telegram_id):
    """
    Возвращает путь к папке медиа файлов пользователя
    """
    return os.path.join(USERS_MEDIA_DIR, str(telegram_id))


def ensure_user_media_dir(telegram_id):
    """
    Создает папку для медиа файлов пользователя если ее нет
    """
    user_dir = get_user_media_dir(telegram_id)
    try:
        os.makedirs(user_dir, exist_ok=True)
        return user_dir
    except Exception as e:
        print(f"❌ Ошибка при создании папки пользователя: {e}")
        return None


def save_user_photo(telegram_id, file_data, filename="profile.jpg"):
    """
    Сохраняет фото пользователя и возвращает путь
    
    Args:
        telegram_id: ID пользователя в Telegram
        file_data: Бинарные данные файла
        filename: Имя файла (по умолчанию profile.jpg)
    
    Returns:
        Относительный путь к файлу или None при ошибке
    """
    try:
        user_dir = ensure_user_media_dir(telegram_id)
        if not user_dir:
            return None
        
        file_path = os.path.join(user_dir, filename)
        
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        # Возвращаем относительный путь от корня проекта
        relative_path = os.path.relpath(file_path, BASE_DIR)
        print(f"✓ Фото сохранено: {relative_path}")
        return relative_path
    except Exception as e:
        print(f"❌ Ошибка при сохранении фото: {e}")
        return None


def delete_user_photo(photo_path):
    """
    Удаляет фото пользователя по пути
    
    Args:
        photo_path: Относительный путь к файлу
    
    Returns:
        True если успешно, False при ошибке
    """
    try:
        full_path = os.path.join(BASE_DIR, photo_path)
        if os.path.exists(full_path):
            os.remove(full_path)
            print(f"✓ Фото удалено: {photo_path}")
            return True
        return False
    except Exception as e:
        print(f"❌ Ошибка при удалении фото: {e}")
        return False


if __name__ == "__main__":
    print("Проверка структуры папок...")
    create_required_directories()
    print("✅ Все папки готовы!")
