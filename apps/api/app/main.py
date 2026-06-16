from fastapi import FastAPI, APIRouter
from .routes import health, auth, mailbox, analytics, auth_google_dev
import uvicorn


app = FastAPI(title="AI Mailbox API")

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(auth_google_dev.router, prefix="/api/v1/auth/google")
app.include_router(mailbox.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")


router = APIRouter()


if __name__ == "__main__":  # pragma: no cover
    # Allow running via `python -m app.main` for local dev
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

