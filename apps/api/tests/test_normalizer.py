import base64

from app.services.ingest.normalizer import _extract_body, _html_to_text, normalize_message


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _part(mime: str, data: str | None = None, parts: list | None = None, filename: str = "") -> dict:
    p: dict = {"mimeType": mime, "filename": filename, "body": {"data": data} if data else {}}
    if parts:
        p["parts"] = parts
    return p


def test_flat_alternative_extracts_both_bodies():
    payload = _part(
        "multipart/alternative",
        parts=[
            _part("text/plain", _b64("plain body")),
            _part("text/html", _b64("<p>html body</p>")),
        ],
    )
    text, html = _extract_body(payload)
    assert text == "plain body"
    assert html == "<p>html body</p>"


def test_nested_mixed_alternative_extracts_both_bodies():
    # The bug: Gmail nests the text parts inside multipart/alternative under
    # multipart/mixed, and the old top-level-only scan found neither.
    payload = _part(
        "multipart/mixed",
        parts=[
            _part(
                "multipart/alternative",
                parts=[
                    _part("text/plain", _b64("nested plain")),
                    _part("text/html", _b64("<p>nested html</p>")),
                ],
            ),
            _part("application/pdf", filename="invoice.pdf"),
        ],
    )
    text, html = _extract_body(payload)
    assert text == "nested plain"
    assert html == "<p>nested html</p>"


def test_single_part_root_plain():
    text, html = _extract_body(_part("text/plain", _b64("just text")))
    assert text == "just text"
    assert html is None


def test_single_part_root_html_derives_text():
    text, html = _extract_body(_part("text/html", _b64("<p>only html</p>")))
    assert html == "<p>only html</p>"
    assert text == "only html"


def test_html_only_fallback_skips_script_and_style():
    doc = (
        "<html><head><style>p { color: red }</style></head>"
        "<body><script>alert('x')</script><p>hello</p><p>world</p></body></html>"
    )
    text, html = _extract_body(_part("text/html", _b64(doc)))
    assert html == doc
    assert text is not None
    assert "hello" in text and "world" in text
    assert "alert" not in text
    assert "color" not in text


def test_attachment_with_text_mimetype_is_skipped():
    payload = _part(
        "multipart/mixed",
        parts=[
            _part("text/plain", _b64("attached notes"), filename="notes.txt"),
            _part("text/plain", _b64("the real body")),
        ],
    )
    text, _ = _extract_body(payload)
    assert text == "the real body"


def test_corrupt_part_does_not_block_sibling():
    payload = _part(
        "multipart/alternative",
        parts=[
            _part("text/plain", "!!not-base64!!"),
            _part("text/html", _b64("<p>still here</p>")),
        ],
    )
    text, html = _extract_body(payload)
    assert html == "<p>still here</p>"
    # The plain part decoded to garbage-or-None; either way html text survives.
    assert text is not None


def test_html_to_text_br_and_blank_collapse():
    assert _html_to_text("line one<br>line two") == "line one\nline two"
    collapsed = _html_to_text("<p>a</p><br><br><br><p>b</p>")
    assert collapsed is not None
    assert "\n\n\n" not in collapsed
    assert _html_to_text("") is None
    assert _html_to_text("<style>only css</style>") is None


def test_normalize_message_end_to_end_nested():
    raw = {
        "id": "msg-1",
        "threadId": "thr-1",
        "snippet": "snip",
        "internalDate": "1751900000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "b@example.com"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "filename": "",
                    "body": {},
                    "parts": [
                        _part("text/plain", _b64("full body text")),
                        _part("text/html", _b64("<p>full body html</p>")),
                    ],
                }
            ],
        },
    }
    n = normalize_message(raw)
    assert n["provider_message_id"] == "msg-1"
    assert n["provider_thread_id"] == "thr-1"
    assert n["body_text"] == "full body text"
    assert n["body_html"] == "<p>full body html</p>"
    assert n["subject"] == "Hello"
