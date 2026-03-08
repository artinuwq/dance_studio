from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ATTENDANCE_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "attendance.py"
BOOKINGS_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "bookings.py"
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"
NEWS_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "news.py"
MEDIA_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "media.py"
PAYMENTS_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "payments.py"


def _window(source: str, marker: str, size: int = 2400) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_get_attendance_has_view_acl_guard():
    source = ATTENDANCE_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def get_attendance(schedule_id):")

    assert "_attendance_view_permission_error(db, schedule)" in window
    assert "if perm_error:" in window
    assert "return perm_error" in window


def test_get_individual_lesson_requires_auth_and_acl():
    source = BOOKINGS_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def get_individual_lesson(lesson_id):")

    assert 'if getattr(g, "telegram_id", None) is None:' in window
    assert '_individual_lesson_view_permission_error(db, lesson)' in window


def test_delete_group_requires_permission_and_dependency_checks():
    source = BOOKINGS_ROUTES.read_text(encoding="utf-8")
    route_window = _window(source, '@bp.route("/api/groups/<int:group_id>", methods=["DELETE"])', size=2600)

    assert "def delete_group(group_id):" in route_window
    assert 'require_permission("manage_schedule")' in route_window
    assert "_collect_group_delete_dependencies(db, group_id)" in route_window
    assert "_format_group_delete_blockers(dependencies)" in route_window
    assert "_send_group_chat_message(" in route_window
    assert "db.delete(group)" in route_window


def test_admin_sensitive_routes_have_permission_checks():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")

    get_user_window = _window(source, "def get_user(user_id):")
    assert 'require_permission("view_all_users")' in get_user_window

    get_staff_window = _window(source, "def get_staff(staff_id):")
    assert 'require_permission("manage_staff", allow_self_staff_id=staff_id)' in get_staff_window

    update_window = _window(source, "def update_staff_from_telegram(telegram_id):")
    assert 'require_permission("manage_staff")' in update_window

    search_staff_window = _window(source, "def search_staff():")
    assert 'require_permission("manage_staff")' in search_staff_window

    search_users_window = _window(source, "def search_users():")
    assert 'require_permission("manage_mailings")' in search_users_window


def test_delete_news_requires_permission():
    source = NEWS_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def delete_news(news_id):")

    assert 'require_permission("create_news")' in window


def test_media_user_photo_routes_check_actor_permissions():
    source = MEDIA_ROUTES.read_text(encoding="utf-8")

    assert "def _photo_permission_error(db, target_user: User):" in source

    upload_window = _window(source, "def upload_user_photo(user_id):")
    assert "_photo_permission_error(db, user)" in upload_window

    delete_window = _window(source, "def delete_user_photo_endpoint(user_id):")
    assert "_photo_permission_error(db, user)" in delete_window

    get_window = _window(source, "def get_user_photo(user_id):")
    assert "_photo_permission_error(db, user)" in get_window


def test_admin_payment_routes_require_manage_schedule_permission():
    source = PAYMENTS_ROUTES.read_text(encoding="utf-8")

    list_window = _window(source, "def admin_list_payments():")
    assert 'require_permission("manage_schedule")' in list_window

    booking_window = _window(source, "def admin_confirm_booking_payment(booking_id: int):")
    assert 'require_permission("manage_schedule")' in booking_window

    abonement_window = _window(source, "def admin_confirm_abonement_payment(abonement_id: int):")
    assert 'require_permission("manage_schedule")' in abonement_window
