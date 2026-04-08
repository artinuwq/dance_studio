from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"


def test_group_schedule_is_exposed_from_public_routes():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")

    assert "def _build_group_schedule_map(db, group_ids):" in source
    assert '"schedule_summary": schedule_info.get("schedule_summary")' in source
    assert '"schedule_slots": schedule_info.get("schedule_slots", [])' in source


def test_frontend_renders_group_schedule_in_cards_and_booking_flow():
    source = FRONTEND.read_text(encoding="utf-8")

    assert "function getGroupScheduleLines(groupData) {" in source
    assert "function renderGroupScheduleHtml(groupData) {" in source
    assert "Когда занятия:" in source
    assert "group-detail-row group-detail-row-stack" in source
    assert "расписание: ${scheduleText}" in source
