import pytest
from dance_studio.core.permissions import has_permission, get_role_name, ROLES

def test_has_permission_valid():
    assert has_permission("владелец", "manage_staff") is True
    assert has_permission("учитель", "cancel_lesson") is True
    assert has_permission("учитель", "manage_staff") is False

def test_has_permission_invalid_role():
    assert has_permission("хакер", "full_system_access") is False

def test_get_role_name():
    assert get_role_name("владелец") == "Владелец"
    assert get_role_name("учитель") == "Учитель"
    assert get_role_name("неизвестный") == "Неизвестная роль"

def test_all_permissions_exist():
    # Проверяем что у тех.админа есть полный доступ
    assert "full_system_access" in ROLES["тех. админ"]["permissions"]
    # Проверяем что у учителя ограниченный список
    assert len(ROLES["учитель"]["permissions"]) < len(ROLES["владелец"]["permissions"])
