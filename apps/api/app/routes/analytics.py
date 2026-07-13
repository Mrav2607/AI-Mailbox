from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.db.models import MailThread, MailMessage, Classification, AppUser

router = APIRouter()


@router.get("/analytics/overview")
# Keep this sync so FastAPI runs the blocking SQLAlchemy calls in its threadpool.
def analytics_overview(
    current_user: AppUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    user_id = current_user.id
    threads_count = db.scalar(select(func.count(MailThread.id)).where(MailThread.user_id == user_id)) or 0
    messages_count = db.scalar(
        select(func.count(MailMessage.id))
        .select_from(MailMessage)
        .join(MailThread, MailMessage.thread_id == MailThread.id)
        .where(MailThread.user_id == user_id)
    ) or 0
    classified_count = db.scalar(
        select(func.count(Classification.id))
        .select_from(Classification)
        .join(MailMessage, Classification.message_id == MailMessage.id)
        .join(MailThread, MailMessage.thread_id == MailThread.id)
        .where(
            MailThread.user_id == user_id,
            # A classifier may persist a row with label=None. That remains
            # unclassified everywhere else and must not inflate this metric.
            Classification.label.is_not(None),
        )
    ) or 0
    return {"summary": {"threads": threads_count, "messages": messages_count, "classified": classified_count}}
