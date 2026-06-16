from celery import Celery
from app.core.config import settings

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
