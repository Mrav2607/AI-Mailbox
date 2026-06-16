from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.db.models import MailThread, MailMessage, Classification

router = APIRouter()


@router.get("/analytics/overview")
async def analytics_overview(user_id: UUID, db: Session = Depends(get_db)) -> dict:
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
        .where(MailThread.user_id == user_id)
    ) or 0
    return {"summary": {"threads": threads_count, "messages": messages_count, "classified": classified_count}}
