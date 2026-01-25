from flask import Flask, jsonify, send_from_directory, request, g, make_response
from datetime import date, time, datetime
import os
import json
import hashlib
from werkzeug.utils import secure_filename
import logging
import uuid
import requests
from sqlalchemy import or_

from backend.db import init_db, get_session, BASE_DIR, Session, engine
from backend.models import Schedule, News, User, Staff, Mailing, Base, Direction, DirectionUploadSession, Group
from backend.media_manager import save_user_photo, delete_user_photo
from backend.permissions import has_permission

# Flask-Admin
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView

# Отключаем SSL/TLS ошибки в логах werkzeug
logging.getLogger('werkzeug').setLevel(logging.ERROR)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(PROJECT_ROOT, "database", "dance.db")}'
app.secret_key = 'dance-studio-secret-key-2026'  # Генерируется для сессий и flash сообщений
init_db()

# ====== Проверка прав по telegram_id ======
def check_permission(telegram_id, permission):
    db = g.db
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff or not staff.position:
        return False
    staff_position = staff.position.strip().lower()
    return has_permission(staff_position, permission)


def require_permission(permission, allow_self_staff_id=None):
    telegram_id = None
    data = request.get_json(silent=True) if request.is_json else None
    if data:
        telegram_id = data.get("actor_telegram_id") or data.get("telegram_id")
    if not telegram_id:
        telegram_id = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")

    if not telegram_id:
        return {"error": "telegram_id обязателен"}, 401

    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    if allow_self_staff_id is not None:
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
        if staff and staff.id == allow_self_staff_id:
            return None

    if not check_permission(telegram_id, permission):
        return {"error": "Нет прав доступа"}, 403

    return None

# Настройка Flask-Admin
class AdminView(AdminIndexView):
    def is_accessible(self):
        return True  # TODO: добавить проверку прав доступа

admin = Admin(app, name='🩰 Dance Studio Admin', index_view=AdminView())

# Добавляем модели в админ-панель
class UserModelView(ModelView):
    column_list = ['id', 'name', 'telegram_id', 'username', 'status', 'phone', 'registered_at']
    column_searchable_list = ['name', 'username', 'telegram_id']
    column_filters = ['status', 'registered_at']
    form_columns = ['telegram_id', 'username', 'name', 'phone', 'email', 'status', 'user_notes', 'staff_notes']

class StaffModelView(ModelView):
    column_list = ['id', 'name', 'position', 'phone', 'telegram_id', 'status']
    column_searchable_list = ['name', 'position', 'telegram_id']
    column_filters = ['position', 'status']
    form_columns = ['name', 'phone', 'email', 'telegram_id', 'position', 'specialization', 'bio', 'teaches', 'status']

class NewsModelView(ModelView):
    column_list = ['id', 'title', 'status', 'created_at']
    column_searchable_list = ['title', 'content']
    column_filters = ['status', 'created_at']
    form_columns = ['title', 'content', 'status', 'photo_path']

class MailingModelView(ModelView):
    column_list = ['mailing_id', 'name', 'status', 'target_type', 'mailing_type', 'created_at']
    column_searchable_list = ['name', 'purpose']
    column_filters = ['status', 'mailing_type', 'target_type', 'created_at']
    form_columns = ['name', 'description', 'purpose', 'status', 'target_type', 'target_id', 'mailing_type', 'scheduled_at']

class ScheduleModelView(ModelView):
    column_list = ['id', 'title', 'teacher_id', 'date', 'start_time', 'end_time', 'status']
    column_searchable_list = ['title']
    column_filters = ['status', 'date']

class DirectionModelView(ModelView):
    column_list = ['direction_id', 'title', 'base_price', 'is_popular', 'status', 'created_at']
    column_searchable_list = ['title', 'description']
    column_filters = ['status', 'is_popular', 'created_at']
    form_columns = ['title', 'description', 'base_price', 'image_path', 'is_popular', 'status']

class DirectionUploadSessionModelView(ModelView):
    column_list = ['session_id', 'admin_id', 'title', 'status', 'created_at']
    column_searchable_list = ['title', 'session_token']
    column_filters = ['status', 'created_at']
    form_columns = ['admin_id', 'title', 'description', 'base_price', 'image_path', 'status', 'session_token']

admin.add_view(UserModelView(User, Session()))
admin.add_view(StaffModelView(Staff, Session()))
admin.add_view(NewsModelView(News, Session()))
admin.add_view(MailingModelView(Mailing, Session()))
admin.add_view(ScheduleModelView(Schedule, Session()))
admin.add_view(DirectionModelView(Direction, Session()))
admin.add_view(DirectionUploadSessionModelView(DirectionUploadSession, Session()))

# Автоматическое управление сессиями
@app.before_request
def before_request():
    g.db = get_session()

@app.teardown_request
def teardown_request(exception):
    db = getattr(g, 'db', None)
    if db is not None:
        db.close()


def format_schedule(s):
    """Форматирует расписание с информацией об учителе"""
    teacher_info = {}
    if s.teacher_staff:
        teacher_info = {
            "id": s.teacher_staff.id,
            "name": s.teacher_staff.name,
            "photo": s.teacher_staff.photo_path
        }
    
    return {
        "id": s.id,
        "title": s.title,
        "teacher_id": s.teacher_id,
        "teacher": teacher_info,
        "date": s.date.isoformat(),
        "start": str(s.start_time),
        "end": str(s.end_time)
    }


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/bot-username")
def get_bot_username():
    """Возвращает имя бота для открытия чата"""
    try:
        # Получаем имя бота из бота если доступно
        from bot.bot import BOT_USERNAME_GLOBAL
        if BOT_USERNAME_GLOBAL:
            return jsonify({"bot_username": BOT_USERNAME_GLOBAL})
        
        # Fallback на конфиг
        from config import BOT_USERNAME
        return jsonify({"bot_username": BOT_USERNAME})
    except:
        return jsonify({"bot_username": "dance_studio_admin_bot"})


@app.route("/schedule")
def schedule():
    db = g.db
    data = db.query(Schedule).all()
    return jsonify([format_schedule(s) for s in data])


@app.route("/schedule", methods=["POST"])
def create_schedule():
    """
    Создает новое занятие
    """
    db = g.db
    data = request.json
    
    if not data.get("title") or not data.get("teacher_id") or not data.get("date") or not data.get("start_time") or not data.get("end_time"):
        return {"error": "title, teacher_id, date, start_time и end_time обязательны"}, 400
    
    teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
    if not teacher:
        return {"error": "Учитель не найден"}, 404
    
    schedule = Schedule(
        title=data["title"],
        teacher_id=data["teacher_id"],
        date=datetime.strptime(data["date"], "%Y-%m-%d").date(),
        start_time=datetime.strptime(data["start_time"], "%H:%M").time(),
        end_time=datetime.strptime(data["end_time"], "%H:%M").time()
    )
    db.add(schedule)
    db.commit()
    
    return format_schedule(schedule), 201


@app.route("/schedule/<int:schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    """
    Обновляет существующее занятие
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    data = request.json
    
    if data.get("title"):
        schedule.title = data["title"]
    if data.get("teacher_id"):
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "Учитель не найден"}, 404
        schedule.teacher_id = data["teacher_id"]
    if data.get("date"):
        schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    if data.get("start_time"):
        schedule.start_time = datetime.strptime(data["start_time"], "%H:%M").time()
    if data.get("end_time"):
        schedule.end_time = datetime.strptime(data["end_time"], "%H:%M").time()
    
    db.commit()
    
    return format_schedule(schedule)


@app.route("/schedule/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    """
    Удаляет занятие
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    schedule.status = "deleted"
    db.commit()
    
    return {"ok": True, "message": "Занятие удалено"}


@app.route("/seed")
def seed():
    db = g.db
    lesson = Schedule(
        title="Балет",
        teacher="Мария",
        date=date.today(),
        start_time=time(18, 0),
        end_time=time(19, 0)
    )
    db.add(lesson)
    db.commit()
    return {"ok": True}


@app.route("/news/manage")
def get_all_news():
    """Получает все новости для управления (включая активные и архивированные)"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = db.query(News).filter(News.status.in_(["active", "archived"])).order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = None
        if n.photo_path:
            photo_url = "/" + n.photo_path.replace("\\", "/")
        
        result.append({
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "photo_path": photo_url,
            "created_at": n.created_at.isoformat(),
            "status": n.status
        })
    
    return jsonify(result)


@app.route("/news", methods=["POST"])
def create_news():
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    if not data.get("title") or not data.get("content"):
        return {"error": "title и content обязательны"}, 400
    
    news = News(
        title=data["title"],
        content=data["content"]
    )
    db.add(news)
    db.commit()
    
    return {
        "id": news.id,
        "title": news.title,
        "content": news.content,
        "photo_path": news.photo_path,
        "created_at": news.created_at.isoformat()
    }, 201


@app.route("/news")
def get_news():
    """Получает только активные новости для главной страницы"""
    db = g.db
    data = db.query(News).filter_by(status="active").order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = None
        if n.photo_path:
            photo_url = "/" + n.photo_path.replace("\\", "/")
        
        result.append({
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "photo_path": photo_url,
            "created_at": n.created_at.isoformat()
        })

    # ETag based on response payload so client can revalidate quickly
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    etag = f"\"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}\""
    client_etag = request.headers.get("If-None-Match")
    if client_etag == etag:
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return resp

    resp = make_response(jsonify(result))
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return resp



@app.route("/news/<int:news_id>/photo", methods=["POST"])
def upload_news_photo(news_id):
    """
    Загружает фото нля новости
    """
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if news.photo_path:
            delete_user_photo(news.photo_path)
        
        # Сохраняем новое фото в папку media
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from backend.media_manager import MEDIA_DIR
        news_dir = os.path.join(MEDIA_DIR, "news", str(news_id))
        os.makedirs(news_dir, exist_ok=True)
        
        file_path = os.path.join(news_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        # Формируем путь: database/media/news/{id}/photo.ext
        photo_path = f"database/media/news/{news_id}/{filename}"
        print(f"📸 Фото сохранено в: {file_path}")
        print(f"📸 Путь в БД: {photo_path}")
        
        news.photo_path = photo_path
        db.commit()
        
        return {
            "id": news.id,
            "photo_path": news.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загружке фото: {e}")
        return {"error": str(e)}, 500
@app.route("/news/<int:news_id>", methods=["DELETE"])
def delete_news(news_id):
    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "deleted"
    db.commit()
    
    return {"ok": True}


@app.route("/news/<int:news_id>/archive", methods=["PUT"])
def archive_news(news_id):
    """Архивирует новость (переводит в статус 'archived')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "archived"
    db.commit()
    
    return {"ok": True}


@app.route("/news/<int:news_id>/restore", methods=["PUT"])
def restore_news(news_id):
    """Восстанавливает новость из архива (переводит статус обратно в 'active')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "active"
    db.commit()
    
    return {"ok": True}


@app.route("/users", methods=["POST"])
def register_user():
    db = g.db
    data = request.json
    
    # Проверяем обязательные поля (только telegram_id и name)
    if not data.get("telegram_id") or not data.get("name"):
        return {"error": "telegram_id и name обязательны"}, 400
    
    # Проверяем, не существует ли пользователь
    existing_user = db.query(User).filter_by(telegram_id=data["telegram_id"]).first()
    if existing_user:
        return {"error": "Пользователь уже зарегистрирован"}, 409
    
    user = User(
        telegram_id=data["telegram_id"],
        username=data.get("username"),
        phone=data.get("phone"),
        name=data["name"],
        email=data.get("email"),
        birth_date=datetime.strptime(data["birth_date"], "%Y-%m-%d").date() if data.get("birth_date") else None,
        user_notes=data.get("user_notes"),
        staff_notes=data.get("staff_notes")
    )
    db.add(user)
    db.commit()
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes
    }, 201


@app.route("/users/<int:telegram_id>", methods=["GET"])
def get_user(telegram_id):
    db = g.db
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes
    }


@app.route("/users/list/all")
def list_all_users():
    db = g.db
    users = db.query(User).order_by(User.registered_at.desc()).all()
    
    return jsonify([
        {
            "id": u.id,
            "telegram_id": u.telegram_id,
            "username": u.username,
            "phone": u.phone,
            "name": u.name,
            "email": u.email,
            "birth_date": u.birth_date.isoformat() if u.birth_date else None,
            "registered_at": u.registered_at.isoformat(),
            "status": u.status,
            "user_notes": u.user_notes,
            "staff_notes": u.staff_notes
        } for u in users
    ])


@app.route("/users/<int:telegram_id>", methods=["PUT"])
def update_user(telegram_id):
    db = g.db
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    data = request.json
    
    if "phone" in data:
        user.phone = data["phone"]
    if "name" in data:
        user.name = data["name"]
    if "email" in data:
        user.email = data["email"]
    if "birth_date" in data and data["birth_date"]:
        user.birth_date = datetime.strptime(data["birth_date"], "%Y-%m-%d").date()
    if "status" in data:
        user.status = data["status"]
    if "user_notes" in data:
        user.user_notes = data["user_notes"]
    if "staff_notes" in data:
        user.staff_notes = data["staff_notes"]
    
    db.commit()
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes,
        "photo_path": user.photo_path
    }


@app.route("/users/<int:telegram_id>/photo", methods=["POST"])
def upload_user_photo(telegram_id):
    """
    Загружает фото пользователя (только для персонала)
    Ожидает файл в form-data с ключом 'photo'
    """
    db = g.db
    
    # Проверяем, является ли пользователь персоналом
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff:
        return {"error": "Загрузка фото доступна только для персонала"}, 403
    
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if user.photo_path:
            delete_user_photo(user.photo_path)
        
        # Сохраняем новое фото
        file_data = file.read()
        filename = "profile." + file.filename.rsplit('.', 1)[1].lower()
        photo_path = save_user_photo(telegram_id, file_data, filename)
        
        if not photo_path:
            return {"error": "Ошибка при сохранении файла"}, 500
        
        # Обновляем БД
        user.photo_path = photo_path
        db.commit()
        
        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "photo_path": user.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        return {"error": str(e)}, 500


@app.route("/users/<int:telegram_id>/photo", methods=["DELETE"])
def delete_user_photo_endpoint(telegram_id):
    """
    Удаляет фото пользователя (только для персонала)
    """
    db = g.db
    
    # Проверяем, является ли пользователь персоналом
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff:
        return {"error": "Удаление фото доступно только для персонала"}, 403
    
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    if not user.photo_path:
        return {"error": "Фото не найдено"}, 404
    
    try:
        delete_user_photo(user.photo_path)
        user.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Фото удалено"}
    
    except Exception as e:
        print(f"Ошибка при удалении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/media/<path:filename>")
def serve_media(filename):
    """
    Служит медиа файлы из папки database/media
    """
    media_dir = os.path.join(PROJECT_ROOT, "database", "media")
    return send_from_directory(media_dir, filename)


@app.route("/database/media/<path:filename>")
def serve_media_full(filename):
    """
    Альтернативный маршрут для полного пути database/media
    """
    base_dir = PROJECT_ROOT
    return send_from_directory(base_dir, "database/media/" + filename)


@app.route("/staff")
def get_all_staff():
    """
    Получить всех сотрудников
    """
    db = g.db
    staff = db.query(Staff).filter_by(status="active").order_by(Staff.created_at.desc()).all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        
        result.append({
            "id": s.id,
            "name": s.name,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "position": s.position,
            "specialization": s.specialization,
            "bio": s.bio,
            "photo_path": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "created_at": s.created_at.isoformat()
        })
    
    return jsonify(result)


@app.route("/staff/check/<int:telegram_id>")
def check_staff_by_telegram(telegram_id):
    """
    Проверить является ли пользователем сотрудником.
    Если данные персонала неполные, подгружает данные из профиля пользователя (без сохранения в БД).
    """
    try:
        db = g.db
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
        
        if not staff:
            return jsonify({
                "is_staff": False,
                "staff": None
            })
        
        # Загружаем профиль пользователя для подстановки данных
        try:
            user = db.query(User).filter_by(telegram_id=telegram_id).first()
        except:
            user = None
        
        # Если данные персонала неполные, берем из профиля пользователя
        staff_data = {
            "id": staff.id,
            "name": staff.name or (user.name if user else None),
            "position": staff.position,
            "specialization": staff.specialization,
            "bio": staff.bio,
            "teaches": staff.teaches,
            "phone": staff.phone,
            "email": staff.email,
            "photo_path": staff.photo_path or (user.photo_path if user else None)
        }
        
        return jsonify({
            "is_staff": True,
            "staff": staff_data
        })
    except Exception as e:
        print(f"⚠️ Ошибка при проверке сотрудника: {e}")
        return jsonify({
            "is_staff": False,
            "staff": None
        })


@app.route("/user/<int:telegram_id>/photo")
def get_user_photo(telegram_id):
    """
    Получить фото, загруженное пользователем через бота
    """
    try:
        db = g.db
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        
        if not user or not user.staff_notes:
            return {"photo_data": None}, 404
        
        # staff_notes содержит base64 фото
        return {
            "photo_data": user.staff_notes
        }
    except Exception as e:
        print(f"⚠️ Ошибка при получении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/staff", methods=["POST"])
def create_staff():
    """
    Создать новый профиль сотрудника.
    Обязательные поля: position, name (или telegram_id с профилем)
    Остальные опциональные.
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    # Получаем имя: либо из данных, либо из профиля пользователя
    staff_name = data.get("name")
    if not staff_name and data.get("telegram_id"):
        user = db.query(User).filter_by(telegram_id=data.get("telegram_id")).first()
        if user and user.name:
            staff_name = user.name
    
    if not staff_name or not data.get("position"):
        return {"error": "name (или telegram_id с профилем) и position обязательны"}, 400

    # Защита от дублей по telegram_id
    if data.get("telegram_id"):
        existing_staff = db.query(Staff).filter_by(telegram_id=data.get("telegram_id")).first()
        if existing_staff:
            return {
                "error": "Пользователь с таким telegram_id уже существует",
                "existing_id": existing_staff.id
            }, 409
    
    # Проверяем допустимые должности
    valid_positions = ["учитель", "администратор", "владелец", "тех. админ"]
    if data.get("position").lower() not in valid_positions:
        return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
    
    teaches_value = normalize_teaches(data.get("teaches"))
    if teaches_value is None:
        teaches_value = 1 if data.get("position").lower() == "учитель" else 0

    staff = Staff(
        name=staff_name,
        phone=data.get("phone") or "+7 000 000 00 00",  # Телефон опциональный
        email=data.get("email"),
        telegram_id=data.get("telegram_id"),
        position=data["position"],
        specialization=data.get("specialization"),
        bio=data.get("bio"),
        teaches=teaches_value,
        status=data.get("status", "active")
    )
    db.add(staff)
    db.commit()

    if data.get("telegram_id"):
        try_fetch_telegram_avatar(data.get("telegram_id"), db, staff_obj=staff)
    
    # Отправляем уведомление в Telegram если есть telegram_id
    if data.get("telegram_id"):
        try:
            import requests
            from config import BOT_TOKEN
            
            position_display = {
                "учитель": "👩‍🏫 Учитель",
                "администратор": "📋 Администратор",
                "владелец": "👑 Владелец",
                "тех. админ": "⚙️ Технический администратор"
            }
            
            position_name = position_display.get(data["position"], data["position"])
            
            message_text = (
                f"🎉 Поздравляем!\n\n"
                f"Вы назначены на должность:\n"
                f"<b>{position_name}</b>\n\n"
                f"в студии танца LISSA DANCE!"
            )
            
            # Отправляем сообщение напрямую через Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": data.get("telegram_id"),
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"✅ Уведомление отправлено пользователю {data.get('telegram_id')}")
            else:
                pass  # print(f"⚠️ Ошибка при отправке уведомления: {response.text}")
                
        except Exception as e:
            pass  # print(f"⚠️ Ошибка при отправке уведомления: {e}")
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }, 201


@app.route("/staff/<int:staff_id>", methods=["GET"])
def get_staff(staff_id):
    """
    Получить информацию о сотруднике
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404

    username = None
    photo_path = staff.photo_path
    if staff.telegram_id:
        user = db.query(User).filter_by(telegram_id=staff.telegram_id).first()
        if user:
            username = user.username
            if not photo_path and user.photo_path:
                photo_path = user.photo_path
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "username": username,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }


@app.route("/staff/update-from-telegram/<int:telegram_id>", methods=["PUT"])
def update_staff_from_telegram(telegram_id):
    """
    Обновляет имя и другие данные персонала из Telegram профиля
    """
    db = g.db
    data = request.json
    
    staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    
    if not staff:
        return {"error": "Персонал не найден"}, 404
    
    if "first_name" in data:
        # Формируем полное имя из first_name и last_name
        name = data["first_name"]
        if data.get("last_name"):
            name += " " + data["last_name"]
        staff.name = name
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "position": staff.position,
        "message": "Имя обновлено из Telegram"
    }


@app.route("/staff/<int:staff_id>", methods=["PUT"])
def update_staff(staff_id):
    """
    Обновить информацию о сотруднике
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    data = request.json
    
    if "name" in data:
        staff.name = data["name"]
    if "phone" in data:
        staff.phone = data["phone"]
    if "email" in data:
        staff.email = data["email"]
    if "telegram_id" in data:
        staff.telegram_id = data["telegram_id"]
    if "position" in data:
        valid_positions = ["Учитель", "администратор", "модератор", "владелец"]
        if data["position"] not in valid_positions:
            return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
        staff.position = data["position"]
    if "specialization" in data:
        staff.specialization = data["specialization"]
    if "bio" in data:
        staff.bio = data["bio"]
    if "teaches" in data:
        staff.teaches = normalize_teaches(data["teaches"])
    if "status" in data:
        staff.status = data["status"]
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }


@app.route("/staff/<int:staff_id>", methods=["DELETE"])
def delete_staff(staff_id):
    """
    Удалить сотрудника
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    staff_name = staff.name
    telegram_id = staff.telegram_id
    
    db.delete(staff)
    db.commit()
    
    # Отправляем уведомление об увольнении в Telegram если есть telegram_id
    if telegram_id:
        try:
            import requests
            from config import BOT_TOKEN
            
            message_text = (
                f"😢 К сожалению...\n\n"
                f"Вы удалены из персонала студии танца LISSA DANCE.\n\n"
                f"Спасибо за сотрудничество!"
            )
            
            # Отправляем сообщение напрямую через Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": telegram_id,
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"✅ Уведомление об увольнении отправлено пользователю {telegram_id}")
            else:
                pass  # print(f"⚠️ Ошибка при отправке уведомления: {response.text}")
                
        except Exception as e:
            pass  # print(f"⚠️ Ошибка при отправке уведомления об увольнении: {e}")
    
    return {
        "message": f"Персонал '{staff_name}' удален",
        "deleted_id": staff_id
    }


@app.route("/staff/<int:staff_id>/photo", methods=["POST"])
def upload_staff_photo(staff_id):
    """
    Загружает фото сотрудника
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if staff.photo_path:
            delete_user_photo(staff.photo_path)
        
        # Сохраняем новое фото в папку teachers
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from backend.media_manager import TEACHERS_MEDIA_DIR
        staff_dir = os.path.join(TEACHERS_MEDIA_DIR, str(staff_id))
        os.makedirs(staff_dir, exist_ok=True)
        
        file_path = os.path.join(staff_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        photo_path = os.path.relpath(file_path, BASE_DIR)
        
        staff.photo_path = photo_path
        db.commit()
        
        return {
            "id": staff.id,
            "photo_path": staff.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        return {"error": str(e)}, 500


@app.route("/staff/<int:staff_id>/photo", methods=["DELETE"])
def delete_staff_photo(staff_id):
    """
    Удаляет фото сотрудника
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    if not staff.photo_path:
        return {"error": "Фото не найдено"}, 404
    
    try:
        delete_user_photo(staff.photo_path)
        staff.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Фото удалено"}
    
    except Exception as e:
        print(f"Ошибка при удалении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/staff/list/teachers")
def list_teachers():
    """
    Возвращает список всех учителей
    """
    db = g.db
    teachers = db.query(Staff).filter(
        Staff.status == "active",
        or_(Staff.position.in_(["учитель", "Учитель"]), Staff.teaches == 1)
    ).all()
    
    result = []
    for t in teachers:
        # Получаем username из User если есть telegram_id
        username = None
        if t.telegram_id:
            user = db.query(User).filter_by(telegram_id=t.telegram_id).first()
            if user:
                username = user.username
        
        result.append({
            "id": t.id,
            "name": t.name,
            "position": t.position,
            "specialization": t.specialization,
            "username": username,
            "teaches": t.teaches,
            "photo": t.photo_path
        })
    
    return jsonify(result)


@app.route("/staff/list/all")
def list_all_staff():
    """
    Возвращает список всего персонала для администраторов
    """
    db = g.db
    staff = db.query(Staff).all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        
        result.append({
            "id": s.id,
            "name": s.name,
            "position": s.position,
            "specialization": s.specialization,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "photo": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "bio": s.bio
        })
    
    return jsonify(result)


@app.route("/staff/search")
def search_staff():
    """
    Поиск пользователей для добавления в персонал.
    Параметры query:
    - q: строка поиска (если не указана, возвращает всех пользователей)
    - by_username: если True, ищет только по юзернейму (используется при @username)
    """
    try:
        db = g.db
        search_query = request.args.get('q', '').strip().lower()
        by_username = request.args.get('by_username', 'false').lower() == 'true'
        
        # Ищем среди пользователей (Users), а не среди персонала (Staff)
        users = db.query(User).all()
        result = []
        
        # Если нет поискового запроса, возвращаем всех пользователей
        if not search_query:
            result = [
                {
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                }
                for u in users
            ]
        else:
            # Выполняем фильтр в зависимости от типа поиска
            for u in users:
                if by_username:
                    # Поиск только по юзернейму (при вводе @username)
                    if u.username:
                        # Нормализуем: убираем @ из обоих строк для сравнения
                        username_clean = u.username.lower().replace('@', '')
                        search_clean = search_query.replace('@', '')
                        if search_clean in username_clean or username_clean.startswith(search_clean):
                            result.append({
                                "id": u.id,
                                "name": u.name,
                                "telegram_id": u.telegram_id,
                                "username": u.username,
                                "phone": u.phone,
                                "email": u.email
                            })
                else:
                    # Поиск по имени или telegram_id (при обычном вводе)
                    if (u.name.lower().startswith(search_query) or 
                        (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                        result.append({
                            "id": u.id,
                            "name": u.name,
                            "telegram_id": u.telegram_id,
                            "username": u.username,
                            "phone": u.phone,
                            "email": u.email
                        })
        
        return jsonify(result)
    except Exception as e:
        print(f"Ошибка при поиске пользователей: {e}")
        return jsonify({"error": str(e)}), 500


# ======================== СИСТЕМА РАССЫЛОК ========================

@app.route("/search-users")
def search_users():
    """Поиск пользователей для рассылок"""
    db = g.db
    try:
        search_query = request.args.get('query', '').strip().lower()
        
        if not search_query:
            return jsonify([]), 200
        
        users = db.query(User).all()
        result = []
        
        for u in users:
            # Поиск по имени или telegram_id
            if (u.name.lower().find(search_query) != -1 or 
                (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                result.append({
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                })
        
        return jsonify(result)
    except Exception as e:
        print(f"Ошибка при поиске пользователей: {e}")
        return jsonify({"error": str(e)}), 500
@app.route("/mailings", methods=["GET"])
def get_mailings():
    """Получает все рассылки (для управления)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailings = db.query(Mailing).order_by(Mailing.created_at.desc()).all()
        
        result = []
        for m in mailings:
            result.append({
                "mailing_id": m.mailing_id,
                "creator_id": m.creator_id,
                "name": m.name,
                "description": m.description,
                "purpose": m.purpose,
                "status": m.status,
                "target_type": m.target_type,
                "target_id": m.target_id,
                "mailing_type": m.mailing_type,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
                "created_at": m.created_at.isoformat()
            })
        
        return jsonify(result)
    except Exception as e:
        print(f"Ошибка при получении рассылок: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/mailings", methods=["POST"])
def create_mailing():
    """Создает новую рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        # Обязательные поля
        if not data.get("creator_id") or not data.get("name") or not data.get("purpose") or not data.get("target_type"):
            return {"error": "creator_id, name, purpose и target_type обязательны"}, 400
        
        # Определяем статус на основе выбора пользователя
        send_now = data.get("send_now", False)
        
        # Если отправляем сейчас, статус = "pending" (ждет отправки)
        # Если отправляем позже, статус = "scheduled"
        status = "pending" if send_now else "scheduled"
        
        # Если нужно отправить сейчас
        sent_at = None
        if send_now:
            sent_at = None  # Отправляется в процессе, sent_at установится после отправки
        
        scheduled_at = data.get("scheduled_at")
        
        # Если это отложенная рассылка, нужно время
        if not send_now and not scheduled_at:
            return {"error": "Для отложенной рассылки требуется scheduled_at"}, 400
        
        # Если scheduled_at передана как строка, конвертируем в datetime
        if scheduled_at and isinstance(scheduled_at, str):
            # Убеждаемся что есть секунды в строке (datetime-local может их не содержать)
            if 'T' in scheduled_at and scheduled_at.count(':') == 1:
                scheduled_at = scheduled_at + ':00'  # Добавляем :00 для секунд
            try:
                scheduled_at = datetime.fromisoformat(scheduled_at)
            except ValueError as e:
                return {"error": f"Неверный формат даты: {e}"}, 400
        
        mailing = Mailing(
            creator_id=data["creator_id"],
            name=data["name"],
            description=data.get("description"),
            purpose=data["purpose"],
            status=status,
            target_type=data["target_type"],
            target_id=data.get("target_id"),
            mailing_type=data.get("mailing_type", "manual"),  # По умолчанию - ручная рассылка
            sent_at=sent_at,
            scheduled_at=scheduled_at
        )
        
        db.add(mailing)
        db.commit()
        
        # Если нужно отправить сейчас, добавляем в очередь отправки
        if send_now:
            from bot.bot import queue_mailing_for_sending
            queue_mailing_for_sending(mailing.mailing_id)
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }, 201
    
    except Exception as e:
        db.rollback()
        print(f"Ошибка при создании рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["GET"])
def get_mailing(mailing_id):
    """Получает информацию о конкретной рассылке"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception as e:
        print(f"Ошибка при получении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["PUT"])
def update_mailing(mailing_id):
    """Обновляет рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Обновляем поля
        if "name" in data:
            mailing.name = data["name"]
        if "description" in data:
            mailing.description = data["description"]
        if "purpose" in data:
            mailing.purpose = data["purpose"]
        if "status" in data:
            mailing.status = data["status"]
        if "target_type" in data:
            mailing.target_type = data["target_type"]
        if "target_id" in data:
            mailing.target_id = data["target_id"]
        if "mailing_type" in data:
            mailing.mailing_type = data["mailing_type"]
        if "sent_at" in data:
            mailing.sent_at = datetime.fromisoformat(data["sent_at"]) if data["sent_at"] else None
        if "scheduled_at" in data:
            mailing.scheduled_at = datetime.fromisoformat(data["scheduled_at"]) if data["scheduled_at"] else None
        
        db.commit()
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception as e:
        db.rollback()
        print(f"Ошибка при обновлении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["DELETE"])
def delete_mailing(mailing_id):
    """Удаляет рассылку (или отменяет её)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Устанавливаем статус "отменено" вместо удаления
        mailing.status = "cancelled"
        db.commit()
        
        return {"message": "Рассылка отменена"}, 200
    
    except Exception as e:
        db.rollback()
        print(f"Ошибка при удалении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>/send", methods=["POST"])
def send_mailing_endpoint(mailing_id):
    """Инициирует отправку рассылки"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    try:
        # Импортируем функцию добавления рассылки в очередь
        from bot.bot import queue_mailing_for_sending
        
        db = g.db
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Проверяем, не отправлена ли уже
        if mailing.status == "sent":
            return {"error": "Рассылка уже была отправлена"}, 400
        
        if mailing.status == "cancelled":
            return {"error": "Рассылка была отменена"}, 400
        
        # Добавляем рассылку в очередь на отправку
        queue_mailing_for_sending(mailing_id)
        
        return {"message": f"Рассылка '{mailing.name}' добавлена в очередь отправки", "status": "pending"}, 200
    
    except Exception as e:
        print(f"Ошибка при отправке рассылки: {e}")
        return {"error": str(e)}, 500


# ======================== СИСТЕМА УПРАВЛЕНИЯ НАПРАВЛЕНИЯМИ ========================

@app.route("/api/directions", methods=["GET"])
def get_directions():
    """Получает все активные направления"""
    db = g.db
    directions = db.query(Direction).filter_by(status="active").order_by(Direction.created_at.desc()).all()
    
    print(f"✓ Найдено {len(directions)} активных направлений")
    
    result = []
    for d in directions:
        image_url = None
        if d.image_path:
            image_url = "/" + d.image_path.replace("\\", "/")
        
        result.append({
            "direction_id": d.direction_id,
            "title": d.title,
            "description": d.description,
            "base_price": d.base_price,
            "is_popular": d.is_popular,
            "image_path": image_url,
            "created_at": d.created_at.isoformat()
        })
    
    return jsonify(result)


@app.route("/api/directions/manage", methods=["GET"])
def get_directions_manage():
    """Получает все направления для управления (включая неактивные)"""
    db = g.db
    directions = db.query(Direction).order_by(Direction.created_at.desc()).all()
    
    result = []
    for d in directions:
        image_url = None
        if d.image_path:
            image_url = "/" + d.image_path.replace("\\", "/")
        
        result.append({
            "direction_id": d.direction_id,
            "title": d.title,
            "description": d.description,
            "base_price": d.base_price,
            "is_popular": d.is_popular,
            "status": d.status,
            "image_path": image_url,
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat()
        })
    
    return jsonify(result)


@app.route("/api/directions/<int:direction_id>", methods=["GET"])
def get_direction(direction_id):
    """Возвращает одно направление по ID для формы редактирования"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    image_url = None
    if direction.image_path:
        image_url = "/" + direction.image_path.replace("\\", "/")

    return jsonify({
        "direction_id": direction.direction_id,
        "title": direction.title,
        "description": direction.description,
        "base_price": direction.base_price,
        "is_popular": direction.is_popular,
        "status": direction.status,
        "image_path": image_url,
        "created_at": direction.created_at.isoformat(),
        "updated_at": direction.updated_at.isoformat()
    })


@app.route("/api/directions/<int:direction_id>/groups", methods=["GET"])
def get_direction_groups(direction_id):
    """Возвращает список групп для направления"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    groups = db.query(Group).filter_by(direction_id=direction_id).order_by(Group.created_at.desc()).all()
    result = []
    for gr in groups:
        teacher_name = gr.teacher.name if gr.teacher else None
        result.append({
            "id": gr.id,
            "direction_id": gr.direction_id,
            "teacher_id": gr.teacher_id,
            "teacher_name": teacher_name,
            "name": gr.name,
            "description": gr.description,
            "age_group": gr.age_group,
            "max_students": gr.max_students,
            "duration_minutes": gr.duration_minutes,
            "created_at": gr.created_at.isoformat()
        })

    return jsonify(result)


@app.route("/api/directions/<int:direction_id>/groups", methods=["POST"])
def create_direction_group(direction_id):
    """Создает группу внутри направления"""
    db = g.db
    data = request.json or {}

    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    name = data.get("name")
    teacher_id = data.get("teacher_id")
    age_group = data.get("age_group")
    max_students = data.get("max_students")
    duration_minutes = data.get("duration_minutes")
    description = data.get("description")

    if not name or not teacher_id or not age_group or not max_students or not duration_minutes:
        return {"error": "name, teacher_id, age_group, max_students, duration_minutes обязательны"}, 400

    teacher = db.query(Staff).filter_by(id=teacher_id).first()
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404

    try:
        max_students_int = int(max_students)
        duration_minutes_int = int(duration_minutes)
    except ValueError:
        return {"error": "max_students и duration_minutes должны быть числами"}, 400

    group = Group(
        direction_id=direction_id,
        teacher_id=teacher_id,
        name=name,
        description=description,
        age_group=age_group,
        max_students=max_students_int,
        duration_minutes=duration_minutes_int
    )
    db.add(group)
    db.commit()

    return {
        "id": group.id,
        "direction_id": group.direction_id,
        "teacher_id": group.teacher_id,
        "teacher_name": teacher.name,
        "name": group.name,
        "description": group.description,
        "age_group": group.age_group,
        "max_students": group.max_students,
        "duration_minutes": group.duration_minutes,
        "created_at": group.created_at.isoformat()
    }, 201


@app.route("/api/groups/<int:group_id>", methods=["GET"])
def get_group(group_id):
    """Возвращает группу по ID"""
    db = g.db
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

    teacher_name = group.teacher.name if group.teacher else None
    return jsonify({
        "id": group.id,
        "direction_id": group.direction_id,
        "teacher_id": group.teacher_id,
        "teacher_name": teacher_name,
        "name": group.name,
        "description": group.description,
        "age_group": group.age_group,
        "max_students": group.max_students,
        "duration_minutes": group.duration_minutes,
        "created_at": group.created_at.isoformat()
    })


@app.route("/api/groups/<int:group_id>", methods=["PUT"])
def update_group(group_id):
    """Обновляет группу"""
    db = g.db
    data = request.json or {}
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

    if "name" in data:
        group.name = data["name"]
    if "description" in data:
        group.description = data["description"]
    if "age_group" in data:
        group.age_group = data["age_group"]
    if "max_students" in data:
        try:
            group.max_students = int(data["max_students"])
        except ValueError:
            return {"error": "max_students должен быть числом"}, 400
    if "duration_minutes" in data:
        try:
            group.duration_minutes = int(data["duration_minutes"])
        except ValueError:
            return {"error": "duration_minutes должен быть числом"}, 400
    if "teacher_id" in data:
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "Преподаватель не найден"}, 404
        group.teacher_id = data["teacher_id"]

    db.commit()

    return {
        "id": group.id,
        "message": "Группа обновлена"
    }


def normalize_teaches(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value else 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "да"):
            return 1
        if v in ("0", "false", "no", "n", "нет"):
            return 0
    return None


def try_fetch_telegram_avatar(telegram_id, db, staff_obj=None):
    """Пробует скачать аватар пользователя из Telegram и сохранить в БД"""
    try:
        from config import BOT_TOKEN
    except Exception:
        return

    try:
        # Получаем фото профиля
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
            params={"user_id": telegram_id, "limit": 1},
            timeout=5
        )
        data = resp.json()
        if not data.get("ok") or data.get("result", {}).get("total_count", 0) == 0:
            return

        file_id = data["result"]["photos"][0][-1]["file_id"]
        file_resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=5
        )
        file_data = file_resp.json()
        if not file_data.get("ok"):
            return

        file_path = file_data["result"]["file_path"]
        photo_resp = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            timeout=10
        )
        if photo_resp.status_code != 200:
            return

        photo_path = save_user_photo(telegram_id, photo_resp.content)
        if not photo_path:
            return

        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user and not user.photo_path:
            user.photo_path = photo_path

        if staff_obj and not staff_obj.photo_path:
            staff_obj.photo_path = photo_path

        db.commit()
    except Exception:
        # Без падения сервера при ошибке сети
        return


@app.route("/api/directions/create-session", methods=["POST"])
def create_direction_upload_session():
    """
    Создает сессию загрузки направления.
    Администратор заполняет форму и получает токен для бота.
    """
    db = g.db
    data = request.json
    
    # Получаем информацию администратора из Telegram
    telegram_user_id = data.get("telegram_user_id")
    if not telegram_user_id:
        return {"error": "telegram_user_id обязателен"}, 400
    
    # Проверяем, что админ - действительно администратор
    admin = db.query(Staff).filter_by(telegram_id=telegram_user_id).first()
    if not admin or admin.position not in ["администратор", "владелец", "тех. админ"]:
        return {"error": "У вас нет прав администратора"}, 403
    
    # Обязательные поля
    required_fields = ["title", "description", "base_price"]
    for field in required_fields:
        if not data.get(field):
            return {"error": f"{field} обязателен"}, 400
    
    # Создаем сессию
    session_token = str(uuid.uuid4())
    
    session = DirectionUploadSession(
        admin_id=admin.id,
        telegram_user_id=telegram_user_id,
        title=data["title"],
        description=data["description"],
        base_price=data["base_price"],
        session_token=session_token,
        status="waiting_for_photo"
    )
    
    db.add(session)
    db.commit()
    
    return {
        "session_id": session.session_id,
        "session_token": session_token,
        "message": "Сессия создана. Отправьте токен боту для загрузки фотографии."
    }, 201


@app.route("/api/directions/upload-complete/<token>", methods=["GET"])
def get_upload_session_status(token):
    """Проверяет статус загрузки фотографии по токену"""
    db = g.db
    
    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        print(f"❌ Сессия не найдена для токена: {token}")
        return {"error": "Сессия не найдена"}, 404
    
    print(f"✓ Статус сессии {token[:8]}...: {session.status}, фото: {session.image_path}")
    
    return {
        "session_id": session.session_id,
        "status": session.status,
        "image_path": "/" + session.image_path.replace("\\", "/") if session.image_path else None,
        "title": session.title,
        "description": session.description,
        "base_price": session.base_price
    }


@app.route("/api/directions", methods=["POST"])
def create_direction():
    """Создает новое направление после загрузки фотографии"""
    db = g.db
    data = request.json
    
    print(f"📝 Запрос на создание направления: {data}")
    
    session_token = data.get("session_token")
    if not session_token:
        return {"error": "session_token обязателен"}, 400
    
    session = db.query(DirectionUploadSession).filter_by(session_token=session_token).first()
    if not session:
        print(f"❌ Сессия не найдена: {session_token}")
        return {"error": "Сессия не найдена"}, 404
    
    print(f"✓ Сессия найдена. Статус: {session.status}, фото: {session.image_path}")
    
    if session.status != "photo_received":
        print(f"❌ Статус не готов. Ожидается 'photo_received', получено: {session.status}")
        return {"error": f"Сессия не готова. Статус: {session.status}"}, 400
    
    # Создаем направление
    direction = Direction(
        title=session.title,
        description=session.description,
        base_price=session.base_price,
        image_path=session.image_path,
        is_popular=data.get("is_popular", 0),
        status="active"
    )
    
    db.add(direction)
    db.commit()
    
    # Обновляем статус сессии
    session.status = "completed"
    db.commit()
    
    print(f"✅ Направление создано: ID={direction.direction_id}, title={direction.title}")
    
    return {
        "direction_id": direction.direction_id,
        "title": direction.title,
        "message": "Направление успешно создано"
    }, 201


@app.route("/api/directions/<int:direction_id>", methods=["PUT"])
def update_direction(direction_id):
    """Обновляет информацию о направлении"""
    db = g.db
    data = request.json
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    # Обновляем поля
    if "title" in data:
        direction.title = data["title"]
    if "description" in data:
        direction.description = data["description"]
    if "base_price" in data:
        direction.base_price = data["base_price"]
    if "status" in data:
        direction.status = data["status"]
    if "is_popular" in data:
        direction.is_popular = data["is_popular"]
    
    db.commit()
    
    return {
        "direction_id": direction.direction_id,
        "message": "Направление обновлено"
    }


@app.route("/api/directions/<int:direction_id>", methods=["DELETE"])
def delete_direction(direction_id):
    """Удаляет направление"""
    db = g.db
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    direction.status = "inactive"
    db.commit()
    
    return {"message": "Направление удалено"}


@app.route("/api/directions/photo/<token>", methods=["POST"])
def upload_direction_photo(token):
    """
    API для загрузки фотографии направления
    Используется ботом при получении фотографии от администратора
    """
    db = g.db
    
    print(f"🔄 Загрузка фотографии для токена: {token[:8]}...")
    
    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        print(f"❌ Сессия не найдена: {token}")
        return {"error": "Сессия не найдена"}, 404
    
    if "photo" not in request.files:
        print(f"❌ Файл не загружен")
        return {"error": "Файл не загружен"}, 400
    
    file = request.files["photo"]
    if file.filename == "":
        print(f"❌ Файл не выбран")
        return {"error": "Файл не выбран"}, 400
    
    try:
        # Создаем директорию для направлений если её нет
        # PROJECT_ROOT = BASE_DIR/.., где BASE_DIR это папка backend
        project_root = os.path.dirname(BASE_DIR)
        directions_dir = os.path.join(project_root, "database", "media", "directions", str(session.session_id))
        os.makedirs(directions_dir, exist_ok=True)
        
        print(f"✓ Директория создана: {directions_dir}")
        
        # Сохраняем файл
        filename = secure_filename(f"photo_{session.session_id}.jpg")
        filepath = os.path.join(directions_dir, filename)
        file.save(filepath)
        
        print(f"✓ Файл сохранен: {filepath}")
        
        # Сохраняем путь в БД относительно корня проекта
        relative_path = os.path.relpath(filepath, project_root)
        session.image_path = relative_path
        session.status = "photo_received"
        db.commit()
        
        print(f"✅ Статус сессии обновлен на 'photo_received'")
        
        return {
            "message": "Фотография загружена",
            "session_id": session.session_id,
            "status": "photo_received"
        }, 200
    
    except Exception as e:
        print(f"Ошибка при загрузке фотографии: {e}")
        return {"error": str(e)}, 500
