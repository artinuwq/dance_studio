from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"
BOOKING_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "bookings.py"


def _window(source: str, marker: str, size: int = 2200) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_public_schedule_frontend_does_not_fan_out_direction_requests():
    source = FRONTEND.read_text(encoding="utf-8")
    window = _window(source, "async function loadPublicSchedule(force = false) {", size=1800)

    assert "await preloadPublicDirections();" not in window
    assert "window.publicScheduleItems = normalized;" in window


def test_group_schedule_ui_reuses_group_direction_payload():
    source = FRONTEND.read_text(encoding="utf-8")
    details_window = _window(source, "function loadGroupScheduleDetails(groupId) {", size=1800)
    defaults_window = _window(source, "function updateGroupFormDefaults() {", size=3400)

    assert "group.direction_title || '—'" in details_window
    assert "group.direction_base_price || null" in details_window
    assert "fetch(`/api/directions/${group.direction_id}`" not in details_window
    assert "rows[rows.length - 2].innerHTML" in defaults_window
    assert "group.direction_base_price ? group.direction_base_price + ' ₽' : '—'" in defaults_window
    assert "fetch(`/api/directions/${group.direction_id}`" not in defaults_window


def test_schedule_public_route_prefetches_related_entities_in_bulk():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def schedule_public():", size=9000)

    assert "groups_by_id" in window
    assert "directions_by_id" in window
    assert "teachers_by_id" in window
    assert "individual_lessons_by_id" in window
    assert "rentals_by_id" in window
    assert "db.query(Group).filter_by(id=s.group_id).first()" not in window
    assert "db.query(Direction).filter_by(direction_id=group.direction_id).first()" not in window
    assert "db.query(Staff).filter_by(id=s.teacher_id).first()" not in window


def test_group_route_exposes_direction_summary_for_schedule_ui():
    source = BOOKING_ROUTES.read_text(encoding="utf-8")
    window = _window(source, 'def get_group(group_id):', size=900)

    assert '"direction_title": direction.title if direction else None' in window
    assert '"direction_base_price": direction.base_price if direction else None' in window
