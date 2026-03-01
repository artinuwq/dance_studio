import pytest
from dance_studio.web.constants import ALLOWED_DIRECTION_TYPES, MAX_UPLOAD_MB

def test_constants():
    """Проверка базовых констант проекта"""
    assert "dance" in ALLOWED_DIRECTION_TYPES
    assert "sport" in ALLOWED_DIRECTION_TYPES
    assert MAX_UPLOAD_MB == 200

def test_logic_mock():
    """Пример теста простой логики"""
    # Здесь могла бы быть проверка функции расчета, 
    # но пока просто убедимся что pytest работает
    result = 100 * 2
    assert result == 200
