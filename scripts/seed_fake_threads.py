"""
Replace threads/messages for a user with fake test data.

Usage:
  $env:DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/ai_mailbox"
  python scripts/seed_fake_threads.py 62e215ff-4536-4d1f-bd71-cbe7bd96b0ff
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/ai_mailbox")
engine = create_engine(DATABASE_URL, future=True)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_fake_threads.py <user_id>")
        raise SystemExit(1)

    user_id = sys.argv[1]
    now = datetime.now(timezone.utc)

    fake_threads = [
        ("Need RSVP for Friday lunch?", "meeting"),
        ("Invoice #1842 is due", "financial"),
        ("Order shipped: Track your package", "orders_shipping"),
        ("Security alert: new login detected", "security_account"),
        ("Can you review the doc by EOD?", "needs_reply"),
        ("Promo: 30% off this weekend", "promotions"),
        ("Weekly product updates", "updates_notifications"),
        ("Follow-up on our last conversation", "follow_up"),
        ("Project kickoff notes", "work_project"),
        ("Family photos from the weekend", "personal"),
        ("Please verify your email", "action_required"),
        ("You won a prize!!!", "spam_junk"),
        ("FYI: interesting article", "other"),
    ]

    with engine.begin() as conn:
        # Delete existing classifications/messages/threads for the user
        conn.execute(
            text(
                """
                DELETE FROM classification
                WHERE message_id IN (
                    SELECT m.id
                    FROM mail_message m
                    JOIN mail_thread t ON t.id = m.thread_id
                    WHERE t.user_id = :user_id
                )
                """
            ),
            {"user_id": user_id},
        )
        conn.execute(
            text(
                """
                DELETE FROM mail_message
                WHERE thread_id IN (
                    SELECT id FROM mail_thread WHERE user_id = :user_id
                )
                """
            ),
            {"user_id": user_id},
        )
        conn.execute(
            text("DELETE FROM mail_thread WHERE user_id = :user_id"),
            {"user_id": user_id},
        )

        # Insert fake threads/messages/classifications
        for idx, (subject, label) in enumerate(fake_threads):
            thread_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            sent_at = now - timedelta(hours=idx * 2)
            conn.execute(
                text(
                    """
                    INSERT INTO mail_thread (id, user_id, provider, provider_thread_id, subject, last_message_at)
                    VALUES (:id, :user_id, :provider, :provider_thread_id, :subject, :last_message_at)
                    """
                ),
                {
                    "id": thread_id,
                    "user_id": user_id,
                    "provider": "fake",
                    "provider_thread_id": f"fake_thread_{idx}",
                    "subject": subject,
                    "last_message_at": sent_at,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mail_message (
                        id, thread_id, provider_message_id, sender, recipient, cc, bcc, sent_at,
                        snippet, body_text, body_html, headers
                    )
                    VALUES (
                        :id, :thread_id, :provider_message_id, :sender, :recipient, :cc, :bcc, :sent_at,
                        :snippet, :body_text, :body_html, :headers
                    )
                    """
                ),
                {
                    "id": message_id,
                    "thread_id": thread_id,
                    "provider_message_id": f"fake_msg_{idx}",
                    "sender": "tester@example.com",
                    "recipient": ["demo@example.com"],
                    "cc": [],
                    "bcc": [],
                    "sent_at": sent_at,
                    "snippet": subject,
                    "body_text": f"Body for: {subject}",
                    "body_html": None,
                    "headers": "{}",
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO classification (id, message_id, label, confidence, rationale, model_version)
                    VALUES (:id, :message_id, :label, :confidence, :rationale, :model_version)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "message_id": message_id,
                    "label": label,
                    "confidence": 0.9,
                    "rationale": "seeded for testing",
                    "model_version": "seeded",
                },
            )

    print(f"Seeded {len(fake_threads)} fake threads for user {user_id}.")


if __name__ == "__main__":
    main()
