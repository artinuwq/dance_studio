from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"


def test_payments_admin_page_has_filters_and_confirm_forms():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="payments-admin"' in source
    assert 'id="payments-filter-user-id"' in source
    assert 'id="payments-filter-date-from"' in source
    assert 'id="payments-filter-date-to"' in source
    assert 'id="payments-filter-type"' in source
    assert 'id="payments-filter-status"' in source
    assert 'id="payments-booking-id"' in source
    assert 'id="payments-abonement-id"' in source
    assert "confirmBookingManualPayment()" in source
    assert "confirmAbonementManualPayment()" in source


def test_payments_admin_is_accessible_from_staff_navigation():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="payments-btn-work"' in source
    assert "showPage('payments-admin')" in source
    assert "if (id === 'payments-admin')" in source
    assert "loadAdminPayments();" in source
