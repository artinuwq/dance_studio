from dance_studio.web.services.text import sanitize_plain_text


def test_sanitize_plain_text_removes_tags_and_script_blocks():
    value = '<script>alert(1)</script><b>Title</b><img src=x onerror=alert(2)>'

    assert sanitize_plain_text(value, multiline=False) == "Title"


def test_sanitize_plain_text_preserves_plain_text_line_breaks():
    value = "<p>Hello</p><div>world</div><br>again"

    assert sanitize_plain_text(value) == "Hello\nworld\nagain"


def test_sanitize_plain_text_returns_none_for_none():
    assert sanitize_plain_text(None) is None
