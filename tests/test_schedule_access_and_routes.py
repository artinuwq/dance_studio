from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"
ATTENDANCE_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "attendance.py"


def _window(source: str, marker: str, size: int = 900) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_legacy_schedule_endpoints_require_manage_schedule():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")

    get_window = _window(source, "def schedule():")
    post_window = _window(source, "def create_schedule():")
    put_window = _window(source, "def update_schedule(schedule_id):")
    delete_window = _window(source, "def delete_schedule(schedule_id):")

    assert 'require_permission("manage_schedule")' in get_window
    assert 'require_permission("manage_schedule")' in post_window
    assert 'require_permission("manage_schedule")' in put_window
    assert 'require_permission("manage_schedule")' in delete_window


def test_schedule_v2_delete_has_real_handler():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")

    marker = '@bp.route("/schedule/v2/<int:schedule_id>", methods=["DELETE"])'
    route_window = _window(source, marker, size=450)

    assert marker in source
    assert "def delete_schedule_v2(schedule_id):" in route_window
    assert "def _resolve_group_active_abonement" not in route_window


def test_schedule_v2_supports_teacher_filter():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def schedule_v2_list():", size=2000)

    assert 'teacher_id = request.args.get("teacher_id")' in window
    assert "Schedule.teacher_id == teacher_id_val" in window


def test_set_attendance_requires_manage_schedule():
    source = ATTENDANCE_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def set_attendance(schedule_id):")

    assert 'require_permission("manage_schedule")' in window


def test_schedule_v2_cancel_route_exists_with_compensation_flow():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    marker = '@bp.route("/schedule/v2/<int:schedule_id>/cancel", methods=["POST"])'
    route_window = _window(source, marker, size=3000)

    assert "def cancel_schedule_v2(schedule_id):" in route_window
    assert 'require_permission("manage_schedule")' in route_window
    assert "_extend_abonement_by_week(" in route_window
    assert "_refund_schedule_attendance_credit(" in route_window
    assert '_send_group_chat_message(' in route_window


def test_schedule_v2_move_route_exists_with_transfer_modes():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    marker = '@bp.route("/schedule/v2/<int:schedule_id>/move", methods=["POST"])'
    route_window = _window(source, marker, size=5000)

    assert "def move_schedule_v2(schedule_id):" in route_window
    assert 'require_permission("manage_schedule")' in route_window
    assert "move_type not in SCHEDULE_MOVE_TYPE_LABELS" in route_window
    assert "if move_type in {\"studio_fault\", \"absence_people\"}" in route_window
    assert "if low_attendance_present_count >= 3" in route_window
