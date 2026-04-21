from pathlib import Path


def test_home_welcome_card_title_is_pinned_closer_to_top():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert ".home-welcome-card {" in source
    assert "margin-top: -24px;" in source
    assert ".home-welcome-card > h3 {" in source
    assert "margin: 0 0 14px 0;" in source


def test_home_welcome_card_title_has_symmetric_spacing_to_status_divider():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert ".home-status-card {" in source
    assert "margin-top: 0;" in source
    assert "padding-top: 12px;" in source


def test_home_logo_stack_is_shifted_up_slightly():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert ".home-logo-standalone {" in source
    assert "margin-top: -10px;" in source
