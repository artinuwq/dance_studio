from sqlalchemy import Column, Integer, String, Date, Time, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)  # Telegram username (@xxx)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    birth_date = Column(Date, nullable=True)
    photo_path = Column(String, nullable=True)  # Путь к фото: media/users/{telegram_id}/profile.jpg
    registered_at = Column(DateTime, default=datetime.now, nullable=False)
    status = Column(String, default="active")  # active, inactive, frozen
    user_notes = Column(Text, nullable=True)  # Заметки пользователя (пожелания)
    staff_notes = Column(Text, nullable=True)  # Заметки персонала


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    telegram_id = Column(Integer, nullable=True)
    # Должности:
    # - "тех. админ" - Технический администратор, все права
    # - "учитель" - Может отменять/переносить занятия, арендовать зал
    # - "администратор" - Создание новостей, курирование расписания
    # - "владелец" - Все права администратора + управление персоналом
    position = Column(String, nullable=False)
    specialization = Column(String, nullable=True)  # Балет, Хип-хоп, Современный танец и т.д.
    bio = Column(Text, nullable=True)  # Описание/биография
    photo_path = Column(String, nullable=True)  # Путь к фото: media/teachers/{id}/photo.jpg
    teaches = Column(Integer, nullable=True)  # Флаг: преподает ли (1/0/NULL)
    status = Column(String, default="active")  # active, on_leave, dismissed
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    
    # Отношение к расписанию
    schedules = relationship("Schedule", back_populates="teacher_staff")


class Schedule(Base):
    __tablename__ = "schedule"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    status = Column(String, default="active")
    
    # Отношение к персоналу
    teacher_staff = relationship("Staff", back_populates="schedules")


class News(Base):
    __tablename__ = "news"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    photo_path = Column(String, nullable=True)  # Путь к фотографии новости
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    status = Column(String, default="active")


# ======================== СИСТЕМА НАПРАВЛЕНИЙ ========================
class Direction(Base):
    __tablename__ = "directions"

    direction_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)  # Название направления (Балет, Хип-хоп, и т.д.)
    description = Column(Text, nullable=True)  # Описание направления
    base_price = Column(Integer, nullable=True)  # Базовая цена
    status = Column(String, default="active")  # active, inactive
    is_popular = Column(Integer, default=0)  # Флаг популярности (0 или 1)
    image_path = Column(String, nullable=True)  # Путь к изображению
    created_at = Column(DateTime, default=datetime.now, nullable=False)  # Дата создания
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)  # Последнее обновление

    # Отношение к группам
    groups = relationship("Group", back_populates="direction")


# ======================== СИСТЕМА ГРУПП ========================
class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    direction_id = Column(Integer, ForeignKey("directions.direction_id"), nullable=False)  # ID направления
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)  # Учитель группы
    name = Column(String, nullable=False)  # Название группы
    description = Column(Text, nullable=True)  # Описание конкретной группы
    age_group = Column(String, nullable=False)  # Возрастная группа (например: "12-16")
    max_students = Column(Integer, nullable=False)  # Максимальное кол-во учеников
    duration_minutes = Column(Integer, nullable=False)  # Обычная длительность в минутах
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    # Отношения
    direction = relationship("Direction", back_populates="groups")
    teacher = relationship("Staff", foreign_keys=[teacher_id])


# ======================== СИСТЕМА ИНДИВИДУАЛЬНЫХ ЗАНЯТИЙ ========================
class IndividualLesson(Base):
    __tablename__ = "individual_lessons"

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)  # ID учителя
    student_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # ID ученика
    duration_minutes = Column(Integer, nullable=False)  # Длительность занятия в минутах
    comment = Column(Text, nullable=True)  # Комментарий к занятию
    person_comment = Column(Text, nullable=True)  # Комментарий человека
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    # Отношения
    teacher = relationship("Staff", foreign_keys=[teacher_id])
    student = relationship("User", foreign_keys=[student_id])


# ======================== СИСТЕМА АРЕНДЫ ЗАЛА ========================
class HallRental(Base):
    __tablename__ = "hall_rentals"

    id = Column(Integer, primary_key=True)
    creator_id = Column(Integer, nullable=False)  # ID создателя аренды
    start_time = Column(DateTime, nullable=False)  # Время начала аренды
    end_time = Column(DateTime, nullable=False)  # Время конца аренды
    purpose = Column(String, nullable=False)  # Назначение (выбор + возможность указать свое)
    payment_status = Column(String, default="pending", nullable=False)  # Статус оплаты (pending, paid, rejected)
    creator_type = Column(String, nullable=False)  # Тип создателя (teacher, user)
    status = Column(String, default="pending", nullable=False)  # Статус (pending, approved, rejected)
    duration_minutes = Column(Integer, nullable=False)  # Длительность аренды в минутах
    comment = Column(Text, nullable=True)  # Комментарий к аренде
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)


# ======================== СИСТЕМА РАССЫЛОК ========================
class Mailing(Base):
    __tablename__ = "mailings"

    mailing_id = Column(Integer, primary_key=True)
    creator_id = Column(Integer, ForeignKey("staff.id"), nullable=False)  # ID создателя
    name = Column(String, nullable=False)  # Название рассылки
    description = Column(Text, nullable=True)  # Описание рассылки
    purpose = Column(String, nullable=False)  # Назначение (информирование, приглашение, напоминание и т.д.)
    # Статусы: pending (на ожидании), scheduled (запланирована), sending (в процессе отправки), 
    # sent (отправлено успешно), failed (отправка не удалась), cancelled (отменено)
    status = Column(String, default="pending", nullable=False)
    # Типы целей: user (Пользователь), group (группа НП/ВГ), direction (направление),
    # tg_chat (Telegram-группа/канал), all (все пользователи)
    target_type = Column(String, nullable=False)
    target_id = Column(String, nullable=True)  # ID цели (для user может быть несколько ID через запятую, для group/direction/tg_chat - одиночный ID)
    # Тип рассылки: manual (ручная, создана человеком), automatic (автоматическая, от системы)
    mailing_type = Column(String, default="manual", nullable=False)
    sent_at = Column(DateTime, nullable=True)  # Время когда рассылка разослана
    scheduled_at = Column(DateTime, nullable=True)  # Когда разослать (для отложенных рассылок)
    created_at = Column(DateTime, default=datetime.now, nullable=False)  # Дата создания

    # Отношение к создателю
    creator = relationship("Staff", foreign_keys=[creator_id])


# ======================== СИСТЕМА ЗАГРУЗКИ ФОТОГРАФИЙ НАПРАВЛЕНИЙ ========================
class DirectionUploadSession(Base):
    __tablename__ = "direction_upload_sessions"

    session_id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey("staff.id"), nullable=False)  # ID администратора
    telegram_user_id = Column(Integer, nullable=False)  # Telegram ID админа для связи с ботом
    title = Column(String, nullable=False)  # Название направления
    description = Column(Text, nullable=True)  # Описание
    base_price = Column(Integer, nullable=True)  # Цена
    image_path = Column(String, nullable=True)  # Путь к загруженному изображению
    status = Column(String, default="waiting_for_photo", nullable=False)  # waiting_for_photo, photo_received, completed, cancelled
    session_token = Column(String, unique=True, nullable=False)  # Уникальный токен сессии для связи с ботом
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    # Отношение к администратору
    admin = relationship("Staff", foreign_keys=[admin_id])
