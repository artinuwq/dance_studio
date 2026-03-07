import re


_SCRIPT_STYLE_BLOCK_RE = re.compile(r"(?is)<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>")
_HTML_COMMENT_RE = re.compile(r"(?is)<!--.*?-->")
_LINE_BREAK_TAG_RE = re.compile(r"(?is)<\s*br\s*/?\s*>")
_BLOCK_CLOSING_TAG_RE = re.compile(r"(?is)</\s*(p|div|li|ul|ol|section|article|h[1-6])\s*>")
_HTML_TAG_RE = re.compile(r"(?is)<[^>]+>")
_INLINE_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def sanitize_plain_text(value, *, multiline=True):
    if value is None:
        return None

    text = str(value).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_COMMENT_RE.sub("", text)
    text = _SCRIPT_STYLE_BLOCK_RE.sub("", text)

    if multiline:
        text = _LINE_BREAK_TAG_RE.sub("\n", text)
        text = _BLOCK_CLOSING_TAG_RE.sub("\n", text)

    text = _HTML_TAG_RE.sub("", text)
    lines = [_INLINE_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]

    if multiline:
        text = "\n".join(line for line in lines if line)
        text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    else:
        text = " ".join(line for line in lines if line)

    return text.strip()
