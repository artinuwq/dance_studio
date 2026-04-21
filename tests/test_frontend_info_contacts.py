from pathlib import Path


def test_info_section_instagram_labels_use_brand_uppercase():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "Instagram • SHEBA SPORT" in source
    assert "Instagram • LISSA DNC" in source
    assert "Instagram • Sheba sport" not in source
    assert "Instagram • Lissa DNC" not in source


def test_info_about_title_uses_top_capsule_pattern():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert 'id="studio-info-capsule" class="action-title-popup studio-info-capsule hidden"' in source
    assert "function showStudioInfoCapsule() {" in source
    assert "function hideStudioInfoCapsule() {" in source
    assert "function syncPageActionCapsules(pageId) {" in source
    assert "if (id === 'info-about') {" in source
    assert "showStudioInfoCapsule();" in source
    assert "syncPageActionCapsules(previousPageId);" in source
    assert '<div class="page info-about-page" id="info-about">' in source
    assert "<h3>Информация о студии</h3>" not in source


def test_info_about_contains_brand_phone_contacts():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "Шеба спорт академия" in source
    assert "+7 999 807 4403" in source
    assert "Lissa DNC" in source
    assert "+7 910 409 0027" in source
    assert "tel:+79998074403" in source
    assert "tel:+79104090027" in source
