from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"


def test_staff_profile_edit_form_no_phone_or_email_inputs():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="staff-profile-phone"' not in source
    assert 'id="staff-profile-email"' not in source


def test_staff_profile_edit_supports_opening_selected_staff_from_personnel():
    source = FRONTEND.read_text(encoding="utf-8")

    assert "openStaffProfileEdit(currentDetailStaffId)" in source
    assert "function resolveStaffProfileEditTargetId()" in source
    assert "async function openStaffProfileEdit(staffId = null)" in source
    assert "const response = await fetch(`/staff/${requestedId}`" in source


def test_staff_profile_edit_supports_role_update_for_manage_staff():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="staff-profile-position-group"' in source
    assert 'id="staff-profile-position"' in source
    assert "function updateStaffProfileTeachesToggle()" in source
    assert "const canEditPosition = ['владелец', 'старший админ', 'тех. админ']" in source
    assert "payload.position = position;" in source


def test_personnel_ui_uses_trainer_role_label_and_teaches_badges():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'value="учитель">👩‍🏫 Тренер</option>' in source
    assert "function getStaffRoleLabel(role)" in source
    assert "if (normalized === 'учитель') return 'Тренер';" in source
    assert "function getStaffTeachesLabel(teaches)" in source
    assert "return teaches ? '👩‍🏫 Преподает' : '👩‍🏫 Не преподает';" in source
