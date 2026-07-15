"""Logging tests: the JSON formatter and the access-log query-string strip.

All offline -- these build LogRecords by hand and assert on the formatted string,
no server or handlers involved.
"""

import json
import logging
from datetime import datetime, timezone

from app.core.logging import JsonFormatter, StripQueryString


def _record(**overrides):
    base = dict(
        name="ai-mailbox",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    base.update(overrides)
    return logging.LogRecord(
        base["name"], base["level"], base["pathname"], base["lineno"],
        base["msg"], base["args"], base["exc_info"],
    )


def test_emits_valid_json_with_canonical_keys():
    out = JsonFormatter().format(_record())
    obj = json.loads(out)  # raises if not valid JSON
    assert obj["level"] == "INFO"
    assert obj["logger"] == "ai-mailbox"
    assert obj["msg"] == "hello world"  # args interpolated
    assert obj["ts"].endswith("Z")


def test_exception_is_one_physical_line_with_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record(exc_info=sys.exc_info())
    out = JsonFormatter().format(rec)
    assert "\n" not in out  # single physical line
    obj = json.loads(out)
    assert "Traceback" in obj["exc"]
    assert "ValueError: boom" in obj["exc"]


def test_non_serializable_extra_does_not_crash():
    # Celery hands us datetimes on records; default=str must catch them.
    rec = _record()
    rec.eta = datetime(2026, 7, 14, tzinfo=timezone.utc)
    obj = json.loads(JsonFormatter().format(rec))
    assert "2026-07-14" in obj["eta"]


def test_extra_cannot_clobber_canonical_keys():
    rec = _record()
    rec.level = "SNEAKY"  # try to overwrite the canonical "level"
    rec.logger = "spoofed"
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["level"] == "INFO"
    assert obj["logger"] == "ai-mailbox"


def test_strip_query_string_removes_secrets_from_access_path():
    rec = _record(
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5", "GET", "/auth/google/callback?code=SECRET&state=X", "1.1", 200),
    )
    assert StripQueryString().filter(rec) is True
    assert rec.args[2] == "/auth/google/callback"
    assert "SECRET" not in JsonFormatter().format(rec)


def test_strip_query_string_leaves_plain_paths_untouched():
    rec = _record(msg='%s %s', args=("GET", "/health"))
    StripQueryString().filter(rec)
    assert rec.args == ("GET", "/health")
