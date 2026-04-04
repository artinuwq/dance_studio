from pathlib import Path


def test_info_section_instagram_labels_use_brand_uppercase():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "Instagram • SHEBA SPORT" in source
    assert "Instagram • LISSA DNC" in source
    assert "Instagram • Sheba sport" not in source
    assert "Instagram • Lissa DNC" not in source
