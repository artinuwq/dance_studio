import pytest
from dance_studio.core.abonement_pricing import (
    _normalize_direction_type,
    _normalize_abonement_type,
    normalize_bundle_group_ids,
    AbonementPricingError
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
    # Группа должна быть в списке
    assert normalize_bundle_group_ids(1, [1, 2, 3]) == [1, 2, 3]
    
    # Ошибка: основной группы нет в бандле
    with pytest.raises(AbonementPricingError, match="group_id must be included"):
        normalize_bundle_group_ids(5, [1, 2, 3])
    
    # Ошибка: дубликаты
    with pytest.raises(AbonementPricingError, match="unique group ids"):
        normalize_bundle_group_ids(1, [1, 1, 2])
