from pathlib import Path


def test_vk_auth_request_uses_shared_auth_headers():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "const response = await fetch('/auth/vk', {" in source
    assert "headers: getAuthHeaders({ 'Content-Type': 'application/json' })," in source
