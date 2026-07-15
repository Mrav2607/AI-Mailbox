import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .core.logging import configure_logging
from .core.errors import register_exception_handlers
from .routes import health, auth, mailbox, analytics, auth_google
import uvicorn

# Configure logging at import, not in lifespan: by the time lifespan runs uvicorn
# has already emitted its startup lines in the default format. This is the
# earliest our code runs when uvicorn imports "app.main:app". (The two banner
# lines uvicorn prints before importing us stay in its own format -- that would
# need a uvicorn --log-config, which isn't worth a config file in the image.)
configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the local classifier off the request path: the ~1 GB encoder used
    # to load lazily on the first classify call, stalling every early request
    # behind it. A daemon thread keeps startup and readiness unblocked, and a
    # missing model degrades exactly as before (try_predict just returns None).
    if (settings.classifier_backend or "auto").lower() in ("local", "auto"):
        from .services.nlp import local_model

        threading.Thread(target=local_model.warmup, name="classifier-warmup", daemon=True).start()
    yield


app = FastAPI(title="AI Mailbox API", lifespan=lifespan)

# Safety-net handlers for DB and uncaught errors (consistent JSON, no leaks).
register_exception_handlers(app)

# Allow the browser frontend(s) to call the API. Origins are configurable via
# CORS_ORIGINS; credentials are enabled so the frontend can send the bearer
# token (and so a future cookie-based flow keeps working).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(auth_google.router, prefix="/api/v1/auth/google")
app.include_router(mailbox.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")


if __name__ == "__main__":  # pragma: no cover
    # Allow running via `python -m app.main` for local dev
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

