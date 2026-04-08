from pathlib import Path


def test_profile_booking_requests_ui_uses_compact_cards_with_expandable_details():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "profile-booking-request-card" in source
    assert "profile-booking-request-details" in source
    assert "<summary>Подробнее</summary>" in source
    assert "const showAmountBefore = Number.isFinite(amountBeforeValue) && Number.isFinite(requestedAmountValue) && amountBeforeValue > requestedAmountValue;" in source
    assert '<div class="small">Скидка: -${escapeHtml(discountAmountText)}</div>' not in source
