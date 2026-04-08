from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"


def test_payments_admin_page_keeps_profiles_and_secondary_switch():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="payments-admin"' in source
    assert 'id="payments-admin-profiles"' in source
    assert 'id="payments-admin-secondary-switch"' in source
    assert 'name="payments-admin-secondary-switch"' in source
    assert "switchAdminSecondaryPaymentProfile(" in source
    assert "Сделать active secondary" not in source
    assert ">Активный secondary<" not in source
    assert "Secondary-профиль можно активировать переключателем выше." not in source
    assert "Сейчас активен secondary-профиль." not in source
    assert "Слот 1 используется как primary fallback." not in source
    assert "Активный дополнительный профиль" in source


def test_payments_admin_hides_legacy_filters_and_manual_confirm_blocks():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="payments-admin-filters-card"' in source
    assert 'id="payments-admin-booking-confirm-card"' in source
    assert 'id="payments-admin-abonement-confirm-card"' in source
    assert 'id="payments-admin-list-card"' in source
    assert 'id="payments-admin-filters-card" style="margin-bottom: 12px; display: none;"' in source
    assert 'id="payments-admin-booking-confirm-card" style="margin-bottom: 12px; display: none;"' in source
    assert 'id="payments-admin-abonement-confirm-card" style="margin-bottom: 12px; display: none;"' in source
    assert 'id="payments-admin-list-card" style="display: none;"' in source


def test_payments_admin_is_accessible_from_staff_navigation():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="payments-btn-work"' in source
    assert "showPage('payments-admin')" in source
    assert "if (id === 'payments-admin')" in source
    assert "loadAdminPayments();" in source
