from dance_studio.core.booking_amounts import compute_non_group_booking_base_amount


def test_compute_non_group_booking_base_amount_for_rental_and_individual(monkeypatch):
    def _fake_get_setting_value(db, key):
        if key == "rental.base_hour_price_rub":
            return 2500
        if key == "individual.base_hour_price_rub":
            return 3000
        raise AssertionError(f"Unexpected key: {key}")

    monkeypatch.setattr("dance_studio.core.booking_amounts.get_setting_value", _fake_get_setting_value)

    assert compute_non_group_booking_base_amount(None, object_type="rental", duration_minutes=90) == 3750
    assert compute_non_group_booking_base_amount(None, object_type="individual", duration_minutes=45) == 2250


def test_compute_non_group_booking_base_amount_rounds_up(monkeypatch):
    monkeypatch.setattr(
        "dance_studio.core.booking_amounts.get_setting_value",
        lambda db, key: 1000,
    )
    assert compute_non_group_booking_base_amount(None, object_type="individual", duration_minutes=61) == 1017


def test_compute_non_group_booking_base_amount_returns_none_for_invalid_inputs():
    assert compute_non_group_booking_base_amount(None, object_type="group", duration_minutes=60) is None
    assert compute_non_group_booking_base_amount(None, object_type="rental", duration_minutes=0) is None
    assert compute_non_group_booking_base_amount(None, object_type="individual", duration_minutes=None) is None
