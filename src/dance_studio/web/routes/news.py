import hashlib
import json
import os

from flask import Blueprint, g, jsonify, make_response, request

from dance_studio.core.media_manager import delete_user_photo
from dance_studio.db.models import News
from dance_studio.web.constants import PROJECT_ROOT
from dance_studio.web.services.access import require_permission
from dance_studio.web.services.media import _build_image_url
bp = Blueprint('news_routes', __name__)


@bp.route("/news/manage")
def get_all_news():
    """РџРѕР»СѓС‡Р°РµС‚ РІСЃРµ РЅРѕРІРѕСЃС‚Рё РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ (РІРєР»СЋС‡Р°СЏ Р°РєС‚РёРІРЅС‹Рµ Рё Р°СЂС…РёРІРёСЂРѕРІР°РЅРЅС‹Рµ)"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = db.query(News).filter(News.status.in_(["active", "archived"])).order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = _build_image_url(n.photo_path)
        
        result.append({
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "photo_path": photo_url,
            "created_at": n.created_at.isoformat(),
            "status": n.status
        })
    
    return jsonify(result)


@bp.route("/news", methods=["POST"])
def create_news():
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    if not data.get("title") or not data.get("content"):
        return {"error": "title Рё content РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400
    
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


@bp.route("/news")
def get_news():
    """РџРѕР»СѓС‡Р°РµС‚ С‚РѕР»СЊРєРѕ Р°РєС‚РёРІРЅС‹Рµ РЅРѕРІРѕСЃС‚Рё РґР»СЏ РіР»Р°РІРЅРѕР№ СЃС‚СЂР°РЅРёС†С‹"""
    db = g.db
    data = db.query(News).filter_by(status="active").order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = _build_image_url(n.photo_path)
        
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


@bp.route("/news/<int:news_id>/photo", methods=["POST"])
def upload_news_photo(news_id):
    """
    Р—Р°РіСЂСѓР¶Р°РµС‚ С„РѕС‚Рѕ РЅР»СЏ РЅРѕРІРѕСЃС‚Рё
    """
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "РќРѕРІРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Р¤Р°Р№Р» РЅРµ РїСЂРµРґРѕСЃС‚Р°РІР»РµРЅ"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Р¤Р°Р№Р» РЅРµ РІС‹Р±СЂР°РЅ"}, 400
    
    # РџСЂРѕРІРµСЂСЏРµРј СЂР°СЃС€РёСЂРµРЅРёРµ
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Р”РѕРїСѓСЃС‚РёРјС‹Рµ С„РѕСЂРјР°С‚С‹: jpg, jpeg, png, gif"}, 400
    
    try:
        # РЈРґР°Р»СЏРµРј СЃС‚Р°СЂРѕРµ С„РѕС‚Рѕ РµСЃР»Рё СЃСѓС‰РµСЃС‚РІСѓРµС‚
        if news.photo_path:
            delete_user_photo(news.photo_path)
        
        # РЎРѕС…СЂР°РЅСЏРµРј РЅРѕРІРѕРµ С„РѕС‚Рѕ РІ РїР°РїРєСѓ media
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from dance_studio.core.media_manager import MEDIA_DIR
        news_dir = os.path.join(MEDIA_DIR, "news", str(news_id))
        os.makedirs(news_dir, exist_ok=True)
        
        file_path = os.path.join(news_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        # Р¤РѕСЂРјРёСЂСѓРµРј РѕС‚РЅРѕСЃРёС‚РµР»СЊРЅС‹Р№ РїСѓС‚СЊ РѕС‚ РєРѕСЂРЅСЏ РїСЂРѕРµРєС‚Р°
        photo_path = os.path.relpath(file_path, PROJECT_ROOT)
        news.photo_path = photo_path
        db.commit()
        
        return {
            "id": news.id,
            "photo_path": _build_image_url(news.photo_path),
            "message": "Р¤РѕС‚Рѕ СѓСЃРїРµС€РЅРѕ Р·Р°РіСЂСѓР¶РµРЅРѕ"
        }, 201
    
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё Р·Р°РіСЂСѓР¶РєРµ С„РѕС‚Рѕ: {e}")
        return {"error": str(e)}, 500


@bp.route("/news/<int:news_id>", methods=["DELETE"])
def delete_news(news_id):
    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "РќРѕРІРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°"}, 404
    
    news.status = "deleted"
    db.commit()
    
    return {"ok": True}


@bp.route("/news/<int:news_id>/archive", methods=["PUT"])
def archive_news(news_id):
    """РђСЂС…РёРІРёСЂСѓРµС‚ РЅРѕРІРѕСЃС‚СЊ (РїРµСЂРµРІРѕРґРёС‚ РІ СЃС‚Р°С‚СѓСЃ 'archived')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "РќРѕРІРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°"}, 404
    
    news.status = "archived"
    db.commit()
    
    return {"ok": True}


@bp.route("/news/<int:news_id>/restore", methods=["PUT"])
def restore_news(news_id):
    """Р’РѕСЃСЃС‚Р°РЅР°РІР»РёРІР°РµС‚ РЅРѕРІРѕСЃС‚СЊ РёР· Р°СЂС…РёРІР° (РїРµСЂРµРІРѕРґРёС‚ СЃС‚Р°С‚СѓСЃ РѕР±СЂР°С‚РЅРѕ РІ 'active')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "РќРѕРІРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°"}, 404
    
    news.status = "active"
    db.commit()
    
    return {"ok": True}



