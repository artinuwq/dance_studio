from dance_studio.core.permissions import ROLES, get_role_name, has_permission


def _find_role_with_permission(permission: str) -> str:
    return next(role for role, spec in ROLES.items() if permission in spec.get("permissions", []))


def _find_role_without_permission(permission: str) -> str:
    return next(role for role, spec in ROLES.items() if permission not in spec.get("permissions", []))


def test_has_permission_valid():
    assert has_permission(_find_role_with_permission("manage_staff"), "manage_staff") is True
    assert has_permission(_find_role_with_permission("cancel_lesson"), "cancel_lesson") is True
    assert has_permission(_find_role_without_permission("manage_staff"), "manage_staff") is False


def test_has_permission_invalid_role():
    assert has_permission("__missing_role__", "full_system_access") is False


def test_get_role_name():
    sample_role = next(iter(ROLES))
    assert get_role_name(sample_role) == ROLES[sample_role]["name"]
    fallback_name = get_role_name("__missing_role__")
    assert isinstance(fallback_name, str)
    assert fallback_name
    assert fallback_name != ROLES[sample_role]["name"]


def test_all_permissions_exist():
    assert any("full_system_access" in spec.get("permissions", []) for spec in ROLES.values())
    permission_sizes = [len(spec.get("permissions", [])) for spec in ROLES.values()]
    assert min(permission_sizes) < max(permission_sizes)
