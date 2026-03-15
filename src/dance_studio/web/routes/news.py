import hashlib
import json
import os

from flask import Blueprint, g, jsonify, make_response, request

from dance_studio.core.media_manager import delete_user_photo
from dance_studio.db.models import News
from dance_studio.web.constants import PROJECT_ROOT
from dance_studio.web.services.access import require_permission
from dance_studio.web.services.api_errors import internal_server_error_response, safe_client_error_message
from dance_studio.web.services.media import _build_image_url
from dance_studio.web.services.text import sanitize_plain_text
from dance_studio.web.services.upload_validation import validate_image_upload

bp = Blueprint("news_routes", __name__)


def _serialize_news_item(news):
    return {
        "id": news.id,
        "title": sanitize_plain_text(news.title, multiline=False) or "",
        "content": sanitize_plain_text(news.content) or "",
        "photo_path": _build_image_url(news.photo_path),
        "created_at": news.created_at.isoformat(),
        "status": news.status,
    }


@bp.route("/news/manage")
def get_all_news():
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = (
        db.query(News)
        .filter(News.status.in_(["active", "archived"]))
        .order_by(News.created_at.desc())
        .all()
    )
    return jsonify([_serialize_news_item(item) for item in data])


@bp.route("/news", methods=["POST"])
def create_news():
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    title = sanitize_plain_text(data.get("title"), multiline=False)
    content = sanitize_plain_text(data.get("content"))

    if not title or not content:
        return {"error": "title и content обязательны"}, 400

    news = News(title=title, content=content)
    db.add(news)
    db.commit()

    payload = _serialize_news_item(news)
    payload["photo_path"] = news.photo_path
    payload.pop("status", None)
    return payload, 201


@bp.route("/news")
def get_news():
    db = g.db
    data = db.query(News).filter_by(status="active").order_by(News.created_at.desc()).all()

    result = []
    for item in data:
        payload = _serialize_news_item(item)
        payload.pop("status", None)
        result.append(payload)

    payload_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    etag = f"\"{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}\""
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


@bp.route("/news/<int:news_id>/photo", methods=["POST"])
def upload_news_photo(news_id):
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()

    if not news:
        return {"error": "Новость не найдена"}, 404

    if "photo" not in request.files:
        return {"error": "Файл не предоставлен"}, 400

    file = request.files["photo"]

    if file.filename == "":
        return {"error": "Файл не выбран"}, 400

    try:
        file_data, detected_ext = validate_image_upload(file)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    try:
        if news.photo_path:
            delete_user_photo(news.photo_path)

        filename = f"photo{detected_ext}"

        from dance_studio.core.media_manager import MEDIA_DIR

        news_dir = os.path.join(MEDIA_DIR, "news", str(news_id))
        os.makedirs(news_dir, exist_ok=True)

        file_path = os.path.join(news_dir, filename)
        with open(file_path, "wb") as file_handle:
            file_handle.write(file_data)

        photo_path = os.path.relpath(file_path, PROJECT_ROOT)
        news.photo_path = photo_path
        db.commit()

        return {
            "id": news.id,
            "photo_path": _build_image_url(news.photo_path),
            "message": "Фото успешно загружено",
        }, 201
    except Exception as exc:
        print(f"Ошибка при загрузке фото: {exc}")
        return internal_server_error_response(
            context="Failed to upload news photo",
            db=db,
        )


@bp.route("/news/<int:news_id>", methods=["DELETE"])
def delete_news(news_id):
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()

    if not news:
        return {"error": "Новость не найдена"}, 404

    news.status = "deleted"
    db.commit()

    return {"ok": True}


@bp.route("/news/<int:news_id>/archive", methods=["PUT"])
def archive_news(news_id):
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


@bp.route("/news/<int:news_id>/restore", methods=["PUT"])
def restore_news(news_id):
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

