from pathlib import Path


def test_admin_booking_requests_ui_is_wired_into_staff_screen():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert 'id="booking-requests-btn-work"' in source
    assert 'id="admin-booking-requests"' in source
    assert "loadAdminBookingRequests()" in source
    assert "openAdminBookingRequestModal(" in source
    assert "openAdminBookingRequestUserProfile(" in source
    assert "approveAdminBookingRequest(" in source
    assert "confirmAdminBookingRequestPayment(" in source
    assert "cancelAdminBookingRequest(" in source
    assert "/api/admin/booking-requests" in source
    assert "/api/admin/booking-requests/${Number(bookingId)}/approve" in source
    assert "/api/admin/booking-requests/${Number(bookingId)}/cancel" in source
    assert "/api/admin/booking-requests/${Number(bookingId)}/confirm-payment" in source
    assert "/users/${normalizedUserId}" in source
    assert "const userLabel = _adminBookingUserLabel(item);" in source
    assert '<div class="profile-booking-request-title">${escapeHtml(userLabel)}</div>' in source
    assert '<div class="admin-booking-request-modal-title">${escapeHtml(userLabel)}</div>' in source
    assert '<div class="admin-booking-request-modal-subtitle">${escapeHtml(typeLabel)}</div>' in source
    assert 'id="admin-booking-request-local-back"' in source
    assert "Открыть профиль пользователя" in source
    assert "const ADMIN_BOOKING_ARCHIVE_AGE_MS = 3 * 24 * 60 * 60 * 1000;" in source
    assert "let adminBookingArchiveVisible = false;" in source
    assert "toggleAdminBookingArchiveVisibility()" in source
    assert "Архив (${archiveItems.length})" in source
    assert "Ожидают оплаты" in source
    assert "Надо подтвердить" in source
    assert "Оплаченные и рассмотренные" in source
