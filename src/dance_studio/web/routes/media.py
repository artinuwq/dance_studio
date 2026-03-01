from pathlib import Path

from flask import Blueprint, g, request, send_from_directory

from dance_studio.core.media_manager import delete_user_photo, save_user_photo
from dance_studio.db.models import Staff, User
from dance_studio.web.constants import FRONTEND_DIR, MEDIA_ROOT, PROJECT_ROOT
bp = Blueprint('media_routes', __name__)


@bp.route("/assets/<path:filename>")
def serve_frontend_asset(filename):
    asset_path = Path(FRONTEND_DIR) / filename
    if asset_path.exists() and asset_path.is_file():
        return send_from_directory(FRONTEND_DIR, filename)
    return {"error": "file not found"}, 404


@bp.route("/users/<int:user_id>/photo", methods=["POST"])
def upload_user_photo(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    if not user.telegram_id:
        return {"error": "telegram_id is not set for this user"}, 400

    staff = db.query(Staff).filter_by(telegram_id=user.telegram_id, status="active").first()
    if not staff:
        return {"error": "upload is allowed only for active staff user"}, 403

    if "photo" not in request.files:
        return {"error": "photo file is required"}, 400

    file = request.files["photo"]
    if file.filename == "":
        return {"error": "filename is empty"}, 400

    allowed_extensions = {"jpg", "jpeg", "png", "gif"}
    if not ("." in file.filename and file.filename.rsplit(".", 1)[1].lower() in allowed_extensions):
        return {"error": "unsupported file extension"}, 400

    try:
        if user.photo_path:
            delete_user_photo(user.photo_path)

        file_data = file.read()
        filename = "profile." + file.filename.rsplit(".", 1)[1].lower()
        photo_path = save_user_photo(user.id, file_data, filename)
        if not photo_path:
            return {"error": "failed to save photo"}, 500

        user.photo_path = photo_path
        db.commit()

        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "photo_path": user.photo_path,
            "message": "photo uploaded",
        }, 201
    except Exception as e:
        return {"error": str(e)}, 500


@bp.route("/users/<int:user_id>/photo", methods=["DELETE"])
def delete_user_photo_endpoint(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    if not user.telegram_id:
        return {"error": "telegram_id is not set for this user"}, 400

    staff = db.query(Staff).filter_by(telegram_id=user.telegram_id, status="active").first()
    if not staff:
        return {"error": "delete is allowed only for active staff user"}, 403

    if not user.photo_path:
        return {"error": "photo not found"}, 404

    try:
        delete_user_photo(user.photo_path)
        user.photo_path = None
        db.commit()
        return {"ok": True, "message": "photo deleted"}
    except Exception as e:
        return {"error": str(e)}, 500


@bp.route("/media/<path:filename>")
def serve_media(filename):
    """
    РЎР»СѓР¶РёС‚ РјРµРґРёР° С„Р°Р№Р»С‹ РёР· var/media; fallback РЅР° СЃС‚Р°СЂС‹Р№ database/media
    """
    var_path = MEDIA_ROOT / filename
    legacy_dir = PROJECT_ROOT / "database" / "media"
    legacy_path = legacy_dir / filename

    if var_path.exists():
        return send_from_directory(var_path.parent, var_path.name)
    if legacy_path.exists():
        return send_from_directory(legacy_dir, filename)
    return {"error": "file not found"}, 404


@bp.route("/database/media/<path:filename>")
def serve_media_full(filename):
    """
    РђР»СЊС‚РµСЂРЅР°С‚РёРІРЅС‹Р№ РјР°СЂС€СЂСѓС‚; РїРѕРґРґРµСЂР¶РёРІР°РµС‚ Рё var/media, Рё СЃС‚Р°СЂС‹Р№ РїСѓС‚СЊ
    """
    var_path = MEDIA_ROOT / filename
    legacy_dir = PROJECT_ROOT / "database" / "media"
    legacy_path = legacy_dir / filename

    if var_path.exists():
        return send_from_directory(var_path.parent, var_path.name)
    if legacy_path.exists():
        return send_from_directory(legacy_dir, filename)
    return {"error": "file not found"}, 404


@bp.route("/user/<int:user_id>/photo")
def get_user_photo(user_id):
    """
    РџРѕР»СѓС‡РёС‚СЊ С„РѕС‚Рѕ, Р·Р°РіСЂСѓР¶РµРЅРЅРѕРµ РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј С‡РµСЂРµР· Р±РѕС‚Р°
    """
    try:
        db = g.db
        user = db.query(User).filter_by(id=user_id).first()
        
        if not user or not user.staff_notes:
            return {"photo_data": None}, 404
        
        # staff_notes СЃРѕРґРµСЂР¶РёС‚ base64 С„РѕС‚Рѕ
        return {
            "photo_data": user.staff_notes
        }
    except Exception as e:
        print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё С„РѕС‚Рѕ: {e}")
        return {"error": str(e)}, 500



