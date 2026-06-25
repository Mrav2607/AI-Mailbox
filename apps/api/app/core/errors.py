"""Global exception handlers.

Routes raise ``HTTPException`` for expected, client-facing errors; FastAPI
already renders those (and request-validation errors) as clean JSON, so we
leave those defaults alone. What we add here is a safety net for the
*unexpected*: database failures and uncaught exceptions. Without it, those
surface as a raw stack trace, which leaks internals and gives the frontend no
consistent shape to parse.

Every response here uses the same ``{"detail": ...}`` envelope FastAPI already
uses, plus an ``error_id`` on 500s so a user-reported error maps to a log line.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .logging import logger


def _error_response(status_code: int, detail: str, error_id: str | None = None) -> JSONResponse:
    body: dict[str, object] = {"detail": detail}
    if error_id is not None:
        body["error_id"] = error_id
    return JSONResponse(status_code=status_code, content=body)


async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    # A constraint violation (e.g. a duplicate unique key) is the client's
    # request conflicting with existing state -- 409, not a 500.
    error_id = uuid.uuid4().hex
    # The exception string embeds the failing SQL and its bound parameters,
    # which can include user data -- keep it out of standard logs and route the
    # full detail to debug, correlated by error_id.
    logger.warning(
        "Integrity error [%s] on %s %s", error_id, request.method, request.url.path,
    )
    logger.debug("Integrity error [%s] detail", error_id, exc_info=exc)
    return _error_response(409, "The request conflicts with existing data.", error_id)


async def _database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    # Any other DB-layer failure means the service can't serve the request
    # right now -- 503, with the details kept server-side.
    error_id = uuid.uuid4().hex
    # As above, the exception can carry SQL + bound params; log only metadata at
    # error level and the full exception at debug.
    logger.error(
        "Database error [%s] on %s %s", error_id, request.method, request.url.path,
    )
    logger.debug("Database error [%s] detail", error_id, exc_info=exc)
    return _error_response(503, "A database error occurred. Please try again later.", error_id)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Last resort: log the full traceback under an id and return a generic 500
    # so we never leak internals to the caller.
    error_id = uuid.uuid4().hex
    logger.error(
        "Unhandled error [%s] on %s %s",
        error_id, request.method, request.url.path, exc_info=exc,
    )
    return _error_response(500, "An internal error occurred.", error_id)


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the safety-net handlers onto the app.

    Order/specificity: ``IntegrityError`` is a subclass of ``SQLAlchemyError``,
    and Starlette dispatches to the most specific registered handler, so both
    are matched correctly. ``HTTPException`` and validation errors keep their
    built-in handlers.
    """
    app.add_exception_handler(IntegrityError, _integrity_error_handler)
    app.add_exception_handler(SQLAlchemyError, _database_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
