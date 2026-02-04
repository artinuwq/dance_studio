from sqlalchemy import Column, Integer, String, Date, Time, DateTime, Text, ForeignKey, Index, CheckConstraint
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
    schedules = relationship("Schedule", back_populates="teacher_staff", foreign_keys="Schedule.teacher_id")


class Schedule(Base):
    __tablename__ = "schedule"

    id = Column(Integer, primary_key=True)
    # Новая архитектура расписания
    object_id = Column(Integer, nullable=True)
    object_type = Column(String, nullable=True)  # group | individual | rental
    date = Column(Date, nullable=True)
    time_from = Column(Time, nullable=True)
    time_to = Column(Time, nullable=True)
    status = Column(String, default="scheduled")  # scheduled | cancelled | moved | completed | pending
    status_comment = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=True)
    updated_by = Column(Integer, ForeignKey("staff.id"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=True)

    # legacy поля (используются текущей логикой)
    title = Column(String, nullable=True)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    
    # Отношение к персоналу
    teacher_staff = relationship("Staff", back_populates="schedules", foreign_keys=[teacher_id])


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
    lessons_per_week = Column(Integer, nullable=True)  # Кол-во занятий в неделю
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    # Отношения
    direction = relationship("Direction", back_populates="groups")
    teacher = relationship("Staff", foreign_keys=[teacher_id])


# ======================== СИСТЕМА ИНДИВИДУАЛЬНЫХ ЗАНЯТИЙ ========================
class IndividualLesson(Base):
    __tablename__ = "individual_lessons"

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)  # ID преподавателя
    student_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # ID ученика
    booking_id = Column(Integer, ForeignKey("booking_requests.id"), nullable=True)
    date = Column(Date, nullable=True)
    time_from = Column(Time, nullable=True)
    time_to = Column(Time, nullable=True)
    teacher_comment = Column(Text, nullable=True)
    student_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    # legacy ????
    duration_minutes = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    person_comment = Column(Text, nullable=True)
    status = Column(String, default="pending", nullable=False)
    status_updated_at = Column(DateTime, nullable=True)
    status_updated_by_id = Column(Integer, ForeignKey("staff.id"), nullable=True)

    # ?????????
    teacher = relationship("Staff", foreign_keys=[teacher_id])
    student = relationship("User", foreign_keys=[student_id])


class HallRental(Base):
    __tablename__ = "hall_rentals"

    id = Column(Integer, primary_key=True)
    creator_id = Column(Integer, nullable=False)  # ID ????????? ??????
    creator_type = Column(String, nullable=False)  # teacher | user
    date = Column(Date, nullable=True)
    time_from = Column(Time, nullable=True)
    time_to = Column(Time, nullable=True)
    purpose = Column(String, nullable=True)
    review_status = Column(String, default="pending", nullable=True)  # pending | approved | rejected
    payment_status = Column(String, default="pending", nullable=True)  # pending | paid | rejected
    activity_status = Column(String, default="pending", nullable=True)  # pending | active | cancelled | completed
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    # legacy ????
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)
    duration_minutes = Column(Integer, nullable=True)


class BookingRequest(Base):
    __tablename__ = "booking_requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user_telegram_id = Column(Integer, nullable=True)
    user_name = Column(String, nullable=True)
    user_username = Column(String, nullable=True)
    object_type = Column(String, nullable=False)  # rental | individual | group
    date = Column(Date, nullable=True)
    time_from = Column(Time, nullable=True)
    time_to = Column(Time, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    lessons_count = Column(Integer, nullable=True)
    group_start_date = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)
    overlaps_json = Column(Text, nullable=True)
    status = Column(String, default="NEW", nullable=False)
    status_updated_by_id = Column(Integer, nullable=True)
    status_updated_by_username = Column(String, nullable=True)
    status_updated_by_name = Column(String, nullable=True)
    status_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=True)

    teacher = relationship("Staff", foreign_keys=[teacher_id])
    group = relationship("Group", foreign_keys=[group_id])


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


# ======================== РАБОЧИЕ ЧАСЫ ПРЕПОДАВАТЕЛЕЙ ========================
class TeacherWorkingHours(Base):
    __tablename__ = "teacher_working_hours"

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    weekday = Column(Integer, nullable=False)  # 0..6 (0=Пн)
    time_from = Column(Time, nullable=False)
    time_to = Column(Time, nullable=False)
    valid_from = Column(Date, nullable=True)  # NULL = всегда
    valid_to = Column(Date, nullable=True)  # NULL = бессрочно
    status = Column(String, default="active", nullable=False)  # active/archived
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    teacher = relationship("Staff", foreign_keys=[teacher_id])

    __table_args__ = (
        Index("ix_teacher_working_hours_teacher_weekday", "teacher_id", "weekday"),
        Index("ix_teacher_working_hours_teacher_validity", "teacher_id", "valid_from", "valid_to"),
    )


# ======================== ИСКЛЮЧЕНИЯ И ОТПУСКА ПРЕПОДАВАТЕЛЕЙ ========================
class TeacherTimeOff(Base):
    __tablename__ = "teacher_time_off"

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    date = Column(Date, nullable=False)
    time_from = Column(Time, nullable=True)  # NULL = весь день
    time_to = Column(Time, nullable=True)  # NULL = весь день
    reason = Column(Text, nullable=True)
    status = Column(String, default="active", nullable=False)  # active/archived
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    teacher = relationship("Staff", foreign_keys=[teacher_id])

    __table_args__ = (
        Index("ix_teacher_time_off_teacher_date", "teacher_id", "date"),
    )


# ======================== ОТМЕТКИ ОТКЛОНЕНИЙ РАСПИСАНИЯ ========================
class ScheduleOverrides(Base):
    __tablename__ = "schedule_overrides"

    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedule.id"), nullable=False)
    override_type = Column(String, nullable=False)  # OUTSIDE_WORKING_HOURS
    reason = Column(Text, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    schedule = relationship("Schedule", foreign_keys=[schedule_id])
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])


class GroupAbonement(Base):
    __tablename__ = "group_abonements"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    balance_credits = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    valid_from = Column(DateTime, nullable=True)
    valid_to = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        CheckConstraint("balance_credits >= 0", name="ck_group_abonements_balance_credits_non_negative"),
    )


class Attendance(Base):
    __tablename__ = "attendance"

    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedule.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String, nullable=False)
    abonement_id = Column(Integer, ForeignKey("group_abonements.id"), nullable=True)
    marked_at = Column(DateTime, nullable=True)
    marked_by_staff_id = Column(Integer, ForeignKey("staff.id"), nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    schedule = relationship("Schedule", foreign_keys=[schedule_id])
    user = relationship("User", foreign_keys=[user_id])
    abonement = relationship("GroupAbonement", foreign_keys=[abonement_id])
    marked_by_staff = relationship("Staff", foreign_keys=[marked_by_staff_id])


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String, default="RUB", nullable=False)
    provider = Column(String, nullable=False)
    status = Column(String, nullable=False)
    description = Column(String, nullable=True)
    meta = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_transactions_amount_positive"),
    )


class GroupAbonementActionLog(Base):
    __tablename__ = "group_abonement_action_logs"

    id = Column(Integer, primary_key=True)
    abonement_id = Column(Integer, ForeignKey("group_abonements.id"), nullable=False)
    action_type = Column(String, nullable=False)
    credits_delta = Column(Integer, nullable=True)
    reason = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    attendance_id = Column(Integer, ForeignKey("attendance.id"), nullable=True)
    payment_id = Column(Integer, ForeignKey("payment_transactions.id"), nullable=True)
    actor_type = Column(String, nullable=False)
    actor_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    payload = Column(Text, nullable=True)

    abonement = relationship("GroupAbonement", foreign_keys=[abonement_id])
    attendance = relationship("Attendance", foreign_keys=[attendance_id])
    payment = relationship("PaymentTransaction", foreign_keys=[payment_id])
