from celery import Celery
from celery.signals import setup_logging
from app.core.config import settings


@setup_logging.connect
def _configure_worker_logging(**kwargs):
    # Connecting to this signal tells Celery NOT to hijack the root logger with
    # its own handlers, so our JSON config survives on the worker. Without it,
    # Celery clears root handlers on startup and the worker logs in plain text.
    # (The worker's --loglevel flag is moot now -- LOG_LEVEL governs.)
    from app.core.logging import configure_logging

    configure_logging()


celery_app = Celery(
    "ai_mailbox",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.tasks_nlp",
        "app.workers.tasks_ingest",
        "app.workers.tasks_actions",
        "app.workers.tasks_email",
    ],
)

celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])

# Server-side sync
# Pruning runs regardless of whether scheduling is on: the browser fallback
# still creates runs when the schedule is disabled, so the table needs tidying
# either way.
celery_app.conf.beat_schedule = {
    "prune-sync-runs": {
        "task": "app.workers.tasks_ingest.prune_sync_runs",
        "schedule": 3600.0,
        "options": {"expires": 3600},
    },
}

if settings.scheduled_sync_interval_seconds > 0:
    celery_app.conf.beat_schedule["dispatch-scheduled-syncs"] = {
        "task": "app.workers.tasks_ingest.dispatch_scheduled_syncs",
        "schedule": float(settings.scheduled_sync_interval_seconds),
        "options": {"expires": settings.scheduled_sync_interval_seconds},
    }
