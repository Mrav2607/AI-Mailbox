"""
Seed demo data for local development so /mail/triage returns real rows.

Usage (from repo root):
    $env:DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/ai_mailbox"
    python scripts/seed_demo_data.py
"""

import os
import uuid
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/ai_mailbox")
engine = create_engine(DATABASE_URL, future=True, json_serializer=lambda obj: obj)


def main() -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        # Create a demo user
        user_id = conn.scalar(
            text(
                """
                INSERT INTO app_user (id, email, display_name)
                VALUES (:id, :email, :name)
                ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """
            ),
            {"id": str(uuid.uuid4()), "email": "demo@example.com", "name": "Demo User"},
        )

        # Create two threads
        thread_templates = [
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "provider": "gmail",
                "provider_thread_id": "t_demo_1",
                "subject": "Project kickoff next week",
                "last_message_at": now - timedelta(hours=3),
            },
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "provider": "gmail",
                "provider_thread_id": "t_demo_2",
                "subject": "Invoice for July",
                "last_message_at": now - timedelta(days=1, hours=2),
            },
        ]
        thread_records = []
        for tv in thread_templates:
            thread_id = conn.scalar(
                text(
                    """
                    INSERT INTO mail_thread (id, user_id, provider, provider_thread_id, subject, last_message_at)
                    VALUES (:id, :user_id, :provider, :provider_thread_id, :subject, :last_message_at)
                    ON CONFLICT (user_id, provider, provider_thread_id) DO UPDATE
                    SET subject = EXCLUDED.subject, last_message_at = EXCLUDED.last_message_at
                    RETURNING id
                    """
                ),
                tv,
            )
            tv_with_id = {**tv, "id": str(thread_id)}
            thread_records.append(tv_with_id)

        # Messages per thread
        message_templates = [
            {
                "id": str(uuid.uuid4()),
                "thread_id": thread_records[0]["id"],
                "provider_message_id": "m_demo_1",
                "sender": "teammate@example.com",
                "recipient": ["demo@example.com"],
                "cc": [],
                "bcc": [],
                "sent_at": now - timedelta(hours=3, minutes=5),
                "snippet": "Can you confirm agenda and attendees?",
                "body_text": "Hi, can you confirm agenda and attendees for kickoff next week?",
                "body_html": None,
                "headers": json.dumps({}),
            },
            {
                "id": str(uuid.uuid4()),
                "thread_id": thread_records[1]["id"],
                "provider_message_id": "m_demo_2",
                "sender": "billing@example.com",
                "recipient": ["demo@example.com"],
                "cc": [],
                "bcc": [],
                "sent_at": now - timedelta(days=1, hours=1),
                "snippet": "Invoice for July attached.",
                "body_text": "Invoice for July attached. Please remit by the 15th.",
                "body_html": None,
                "headers": json.dumps({}),
            },
        ]
        message_records = []
        for template in message_templates:
            payload = {**template, "headers": json.dumps({})}
            message_id = conn.scalar(
                text(
                    """
                    INSERT INTO mail_message (
                        id, thread_id, provider_message_id, sender, recipient, cc, bcc, sent_at,
                        snippet, body_text, body_html, headers
                    )
                    VALUES (
                        :id, :thread_id, :provider_message_id, :sender, :recipient, :cc, :bcc, :sent_at,
                        :snippet, :body_text, :body_html, CAST(:headers AS jsonb)
                    )
                    ON CONFLICT (thread_id, provider_message_id) DO UPDATE SET
                        sender = EXCLUDED.sender,
                        snippet = EXCLUDED.snippet,
                        body_text = EXCLUDED.body_text,
                        sent_at = EXCLUDED.sent_at
                    RETURNING id
                    """
                ),
                payload,
            )
            message_records.append({**template, "id": str(message_id)})

        # Classification labels for the latest messages
        classifications = [
            {
                "id": str(uuid.uuid4()),
                "message_id": message_records[0]["id"],
                "label": "needs_reply",
                "confidence": 0.83,
                "rationale": "Action requested for agenda confirmation",
                "model_version": "demo-1",
            },
            {
                "id": str(uuid.uuid4()),
                "message_id": message_records[1]["id"],
                "label": "transactional",
                "confidence": 0.92,
                "rationale": "Contains invoice/payment request",
                "model_version": "demo-1",
            },
        ]
        for c in classifications:
            conn.execute(
                text(
                    """
                    INSERT INTO classification (id, message_id, label, confidence, rationale, model_version)
                    VALUES (:id, :message_id, :label, :confidence, :rationale, :model_version)
                    """
                ),
                c,
            )

        print("Seeded demo user, threads, messages, and classifications.")


if __name__ == "__main__":
    main()
