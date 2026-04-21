from pathlib import Path


def test_group_booking_combo_benefit_is_rendered_as_separate_card():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert 'id="group-booking-combo-benefit"' in source
    assert "function getGroupBookingComboBenefitSnapshot()" in source
    assert "Number(independentQuote.amount_before_discount)" in source
    assert "Number(groupBookingState.comboQuote.amount_before_discount)" in source
    assert "Персональная скидка считается отдельно." in source
