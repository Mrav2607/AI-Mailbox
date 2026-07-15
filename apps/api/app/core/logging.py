"""Logging setup: one JSON object per line to stdout, or human-readable text.

`configure_logging()` owns the config and is called once at process start (from
`app.main` for the API, and from a Celery `setup_logging` signal for the worker).
The format is chosen by `settings` -- JSON in production so a log shipper or
`docker compose logs | jq` can parse it, text locally so it stays readable.

Everything hangs one handler on the root logger and lets named loggers propagate
into it, so there's a single place records get formatted and no double-emit. The
one exception is `uvicorn.access`, which gets its own handler carrying a filter
that strips the query string -- otherwise the OAuth `code`/`state` and mailbox
search terms that ride in as GET params would land in the access log.
"""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger("ai-mailbox")

_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"

# Attributes every LogRecord carries. Anything on a record NOT in here is treated
# as caller-supplied structured context (logger.info(..., extra={...})) and merged
# into the JSON. `color_message` is uvicorn's ANSI-decorated duplicate of msg --
# we don't want it. `taskName` shows up on 3.12+ and is noise here.
_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"color_message", "taskName", "asctime", "message"}


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object.

    `default=str` is the safety net: Celery hands us records whose `extra`
    carries datetimes (task ETAs) and other non-JSON types, and one unserializable
    value would otherwise raise inside logging and drop the record entirely.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge structured extras, but never let them clobber the canonical keys.
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            # json.dumps escapes the newlines, so the multi-line traceback still
            # serializes to one physical line.
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StripQueryString(logging.Filter):
    """Drop the `?...` off the request path in uvicorn access records.

    uvicorn logs access lines as `'%s - "%s %s HTTP/%s" %d'` with the path as the
    third positional arg. The OAuth callback and mailbox search take secrets and
    search terms as query params, so we cut the query before it's formatted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def configure_logging() -> None:
    """Install our handlers/formatters. Idempotent -- safe to call more than once."""
    fmt = settings.effective_log_format
    level = settings.log_level.upper()

    formatter = (
        {"()": f"{__name__}.JsonFormatter"}
        if fmt == "json"
        else {"format": _TEXT_FORMAT}
    )

    logging.config.dictConfig(
        {
            "version": 1,
            # Don't tear down loggers already created at import time (ours, the
            # modules that grabbed `logger`, uvicorn's) -- just reconfigure.
            "disable_existing_loggers": False,
            "formatters": {"default": formatter},
            "filters": {"strip_qs": {"()": f"{__name__}.StripQueryString"}},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                },
                "access": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                    "filters": ["strip_qs"],
                },
            },
            # One handler on root; everyone else propagates into it. This keeps a
            # single format everywhere and lets pytest's caplog (which attaches to
            # root) see our records -- so named loggers must NOT set propagate False.
            "root": {"level": level, "handlers": ["default"]},
            "loggers": {
                "ai-mailbox": {"level": level, "handlers": [], "propagate": True},
                "uvicorn": {"level": level, "handlers": [], "propagate": True},
                "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
                # Own handler + no propagate: it needs the query-strip filter, and
                # routing it through root as well would log every request twice.
                "uvicorn.access": {
                    "level": level,
                    "handlers": ["access"],
                    "propagate": False,
                },
                "celery": {"level": level, "handlers": [], "propagate": True},
            },
        }
    )
