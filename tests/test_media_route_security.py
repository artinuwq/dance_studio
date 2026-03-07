from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid

from flask import Flask

from dance_studio.web.routes import media as media_routes


def _build_response(app: Flask, result):
    response = app.make_response(result)
    response.direct_passthrough = False
    return response


def _configure_media_roots(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    media_root = tmp_path / "var" / "media"
    project_root = tmp_path
    media_root.mkdir(parents=True)
    (project_root / "database" / "media").mkdir(parents=True)

    monkeypatch.setattr(media_routes, "MEDIA_ROOT", media_root)
    monkeypatch.setattr(media_routes, "PROJECT_ROOT", project_root)
    return media_root, project_root


@contextmanager
def _workspace_temp_dir():
    temp_root = Path.cwd() / "var" / "media_route_security_cases"
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    case_root = temp_root / uuid.uuid4().hex
    case_root.mkdir(parents=True, exist_ok=False)
    try:
        yield case_root
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_serve_media_returns_file_inside_media_root(monkeypatch):
    with _workspace_temp_dir() as project_root:
        media_root, _ = _configure_media_roots(monkeypatch, project_root)
        target = media_root / "teachers" / "profile.txt"
        target.parent.mkdir(parents=True)
        target.write_text("ok", encoding="utf-8")

        app = Flask(__name__)
        with app.test_request_context("/media/teachers/profile.txt"):
            response = _build_response(app, media_routes.serve_media("teachers/profile.txt"))

        assert response.status_code == 200
        assert response.get_data(as_text=True) == "ok"


def test_serve_media_blocks_path_traversal(monkeypatch):
    with _workspace_temp_dir() as project_root:
        _, project_root = _configure_media_roots(monkeypatch, project_root)
        (project_root / ".env").write_text("BOT_TOKEN=secret", encoding="utf-8")

        app = Flask(__name__)
        with app.test_request_context("/media/../../.env"):
            response = _build_response(app, media_routes.serve_media("../../.env"))

        assert response.status_code == 404


def test_serve_media_full_returns_file_inside_legacy_root(monkeypatch):
    with _workspace_temp_dir() as project_root:
        _, project_root = _configure_media_roots(monkeypatch, project_root)
        target = project_root / "database" / "media" / "news" / "item.txt"
        target.parent.mkdir(parents=True)
        target.write_text("legacy", encoding="utf-8")

        app = Flask(__name__)
        with app.test_request_context("/database/media/news/item.txt"):
            response = _build_response(app, media_routes.serve_media_full("news/item.txt"))

        assert response.status_code == 200
        assert response.get_data(as_text=True) == "legacy"


def test_serve_media_full_blocks_path_traversal(monkeypatch):
    with _workspace_temp_dir() as project_root:
        _, project_root = _configure_media_roots(monkeypatch, project_root)
        (project_root / ".env").write_text("BOT_TOKEN=secret", encoding="utf-8")

        app = Flask(__name__)
        with app.test_request_context("/database/media/../../.env"):
            response = _build_response(app, media_routes.serve_media_full("../../.env"))

        assert response.status_code == 404
