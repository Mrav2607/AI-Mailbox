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
    ],
)

celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])
