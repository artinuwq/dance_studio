from pathlib import Path

from flask import Blueprint, g, request, send_from_directory
from werkzeug.utils import safe_join

from dance_studio.core.media_manager import delete_user_photo, save_user_photo
from dance_studio.db.models import Staff, User
from dance_studio.web.constants import FRONTEND_DIR, MEDIA_ROOT, PROJECT_ROOT
from dance_studio.web.services.access import get_current_user_from_request, require_permission
from dance_studio.web.services.api_errors import internal_server_error_response

bp = Blueprint("media_routes", __name__)


def _photo_permission_error(db, target_user: User):
    if getattr(g, "telegram_id", None) is None:
        return {"error": "auth required"}, 401

    current_user = get_current_user_from_request(db)
    if current_user and current_user.id == target_user.id:
        return None

    return require_permission("manage_staff")


def _serve_from_root_if_exists(root: Path, filename: str):
    safe_path = safe_join(str(root), filename)
    if not safe_path:
        return None

    candidate = Path(safe_path)
    if not candidate.exists() or not candidate.is_file():
        return None

    return send_from_directory(str(root), filename)


@bp.route("/assets/<path:filename>")
def serve_frontend_asset(filename):
    asset_path = Path(FRONTEND_DIR) / filename
    if asset_path.exists() and asset_path.is_file():
        return send_from_directory(FRONTEND_DIR, filename)
    return {"error": "file not found"}, 404


@bp.route("/users/<int:user_id>/photo", methods=["POST"])
def upload_user_photo(user_id):
    db = g.db
    if getattr(g, "telegram_id", None) is None:
        return {"error": "auth required"}, 401

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    perm_error = _photo_permission_error(db, user)
    if perm_error:
        return perm_error

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
    except Exception:
        return internal_server_error_response(
            context="Failed to upload user photo",
            db=db,
        )


@bp.route("/users/<int:user_id>/photo", methods=["DELETE"])
def delete_user_photo_endpoint(user_id):
    db = g.db
    if getattr(g, "telegram_id", None) is None:
        return {"error": "auth required"}, 401

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    perm_error = _photo_permission_error(db, user)
    if perm_error:
        return perm_error

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
    except Exception:
        return internal_server_error_response(
            context="Failed to delete user photo",
            db=db,
        )


@bp.route("/media/<path:filename>")
def serve_media(filename):
    legacy_dir = PROJECT_ROOT / "database" / "media"

    response = _serve_from_root_if_exists(MEDIA_ROOT, filename)
    if response is not None:
        return response

    response = _serve_from_root_if_exists(legacy_dir, filename)
    if response is not None:
        return response

    return {"error": "file not found"}, 404


@bp.route("/database/media/<path:filename>")
def serve_media_full(filename):
    legacy_dir = PROJECT_ROOT / "database" / "media"

    response = _serve_from_root_if_exists(MEDIA_ROOT, filename)
    if response is not None:
        return response

    response = _serve_from_root_if_exists(legacy_dir, filename)
    if response is not None:
        return response

    return {"error": "file not found"}, 404
