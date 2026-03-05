import pytest

from dance_studio.core.abonement_pricing import (
    AbonementPricingError,
    _normalize_abonement_type,
    _normalize_direction_type,
    _normalize_multi_lessons_per_group,
    _resolve_multi_lessons_per_group,
    _resolve_multi_single_amount_with_fallback,
    normalize_bundle_group_ids,
)


def test_normalize_direction_type():
    assert _normalize_direction_type("  DANCE  ") == "dance"
    assert _normalize_direction_type("sport") == "sport"
    with pytest.raises(AbonementPricingError):
        _normalize_direction_type("yoga")


def test_normalize_abonement_type():
    assert _normalize_abonement_type("MULTI") == "multi"
    assert _normalize_abonement_type("single") == "single"
    with pytest.raises(AbonementPricingError):
        _normalize_abonement_type("invalid")


def test_normalize_bundle_group_ids():
    assert normalize_bundle_group_ids(1, [1, 2, 3]) == [1, 2, 3]

    with pytest.raises(AbonementPricingError, match="group_id must be included"):
        normalize_bundle_group_ids(5, [1, 2, 3])

    with pytest.raises(AbonementPricingError, match="unique group ids"):
        normalize_bundle_group_ids(1, [1, 1, 2])


def test_normalize_multi_lessons_per_group():
    assert _normalize_multi_lessons_per_group(4) == 4
    assert _normalize_multi_lessons_per_group("16") == 16
    with pytest.raises(AbonementPricingError, match="4, 8, 12, 16"):
        _normalize_multi_lessons_per_group(10)


def test_resolve_multi_lessons_per_group_for_single_group_allows_16():
    payloads = [{"lessons_per_week": 4}]
    assert _resolve_multi_lessons_per_group(payloads, bundle_size=1, requested_lessons_per_group=None) == 16
    assert _resolve_multi_lessons_per_group(payloads, bundle_size=1, requested_lessons_per_group=16) == 16


def test_resolve_multi_lessons_per_group_for_bundle_caps_and_validates():
    payloads = [{"lessons_per_week": 4}, {"lessons_per_week": 4}]
    assert _resolve_multi_lessons_per_group(payloads, bundle_size=2, requested_lessons_per_group=None) == 12
    assert _resolve_multi_lessons_per_group(payloads, bundle_size=2, requested_lessons_per_group=12) == 12
    with pytest.raises(AbonementPricingError, match="one of: 4, 8, 12"):
        _resolve_multi_lessons_per_group(payloads, bundle_size=2, requested_lessons_per_group=16)


def test_multi_single_amount_fallback_uses_default_studio_matrix(monkeypatch):
    def _fake_get_setting_value(db, key):
        if key == "abonements.multi_single_prices_json":
            return {}
        if key == "abonements.multi_bundle_prices_json":
            return {}
        raise AssertionError(f"Unexpected key: {key}")

    monkeypatch.setattr("dance_studio.core.abonement_pricing.get_setting_value", _fake_get_setting_value)

    assert _resolve_multi_single_amount_with_fallback(None, direction_type="dance", lessons_per_group=16) == 14400


def test_bundle_default_matrix_contains_expected_tariffs():
    from dance_studio.core.abonement_pricing import DEFAULT_MULTI_BUNDLE_PRICES

    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["2"]["4"] == 6400
    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["2"]["8"] == 12800
    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["2"]["12"] == 19200
    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["3"]["4"] == 8400
    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["3"]["8"] == 16800
    assert DEFAULT_MULTI_BUNDLE_PRICES["dance"]["3"]["12"] == 25200
