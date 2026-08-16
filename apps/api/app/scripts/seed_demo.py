"""
Seed a user's mailbox with sample threads so the app has something to show.

Without this, a fresh `docker compose up` ends at an empty console: no mail is
connected, so every bucket reads zero and there's nothing to evaluate. The
demo-login route calls `seed_demo_data` on every demo login (dev only) --
it's idempotent, so an already-seeded user is a no-op and one that a prior
best-effort attempt left empty gets healed on the next login. This module
also doubles as a CLI for seeding an existing account:

    python -m app.scripts.seed_demo you@example.com
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import AppUser, Classification, MailMessage, MailThread, ProviderAccount

# The fake connection that owns the sample threads. mail_thread.provider_account_id
# is NOT NULL, so seeded mail needs an account row to hang off.
DEMO_ACCOUNT_EMAIL = "demo@cortexmail.local"
# Every demo account shared this one literal before it was scoped per user.
# provider_account has TWO unique constraints (models/provider.py): one keyed
# on (user_id, provider, external_user_id), but the other on just (provider,
# external_user_id) -- NOT scoped by user. A shared literal means the SECOND
# demo user to sign in always violates that second constraint. Kept around
# (rather than deleted) so accounts seeded before this fix are still found by
# _demo_account's lookup instead of getting a second, colliding row.
LEGACY_DEMO_EXTERNAL_ID = "demo-seed-account"
DEMO_MODEL_VERSION = "demo-seed"


def _demo_external_id(user_id: uuid.UUID) -> str:
    """The external id a NEW demo account gets: unique per user, so two demo
    users can never collide on provider_account's (provider, external_user_id)
    constraint the way the old shared literal did."""
    return f"demo-seed-account-{user_id}"

# (subject, sender, snippet, label, confidence, hours_ago). Confidences span the
# whole range on purpose: the confidence bar's low tier only paints at 35% or
# below, and a demo where everything scores 0.9 hides half the UI.
_SAMPLE: tuple[tuple[str, str, str, str | None, float | None, int], ...] = (
    (
        "Re: contract redline, final pass?",
        "alice@stripe.com",
        "Legal signed off on everything except section 4. Can you confirm the "
        "payment terms before Thursday so we can countersign?",
        "needs_reply",
        0.93,
        1,
    ),
    (
        "Invoice #4821 due Friday",
        "billing@vercel.com",
        "Your invoice for $480.00 is due on Friday. No action needed if you "
        "have autopay enabled.",
        "action_required",
        0.88,
        4,
    ),
    (
        "New sign-in from Chrome on Ubuntu",
        "security@google.com",
        "We noticed a new sign-in to your Google Account. If this was you, you "
        "can safely ignore this email.",
        "security_alert",
        0.96,
        7,
    ),
    (
        "Standup notes — engineering",
        "team@linear.app",
        "Yesterday: shipped the ingest retry fix. Today: schema migration "
        "review. Blockers: none.",
        "fyi",
        0.71,
        11,
    ),
    (
        "Can you review PR #4821 today?",
        "bob@figma.com",
        "It's a small diff but it touches the auth path, so I'd rather not "
        "merge it without a second pair of eyes.",
        "needs_reply",
        0.84,
        20,
    ),
    (
        "50% off this weekend only",
        "deals@uber.com",
        "Spring sale ends Sunday. Use code SPRING50 at checkout for half off "
        "your next three rides.",
        "promotional",
        0.91,
        27,
    ),
    (
        "Please sign the updated contractor agreement",
        "carol@acme.io",
        "Same terms as last year, we just updated the IP assignment clause. "
        "DocuSign link expires in 7 days.",
        "action_required",
        0.79,
        33,
    ),
    (
        "Your weekly digest is here",
        "newsletter@vercel.com",
        "Top stories this week, new launches, and tutorials curated for you.",
        "promotional",
        0.62,
        41,
    ),
    (
        "Quick question about the migration",
        "ops@github.com",
        "Does the 0021 migration need a downtime window, or is it safe to run "
        "against a live database?",
        "needs_reply",
        0.33,
        50,
    ),
    (
        "Unusual activity on your account",
        "no-reply@notion.so",
        "We detected a login from a new device. Review recent activity to make "
        "sure it was you.",
        "security_alert",
        0.58,
        63,
    ),
    (
        "Lunch Friday?",
        "dave@example.com",
        "A few of us are heading to the place on 5th around noon. You in?",
        "fyi",
        0.28,
        72,
    ),
    (
        "RSVP: design review sync",
        "team@linear.app",
        "Thursday 2pm, 45 minutes. Agenda is in the doc. Let me know if you "
        "can't make it.",
        "action_required",
        0.67,
        88,
    ),
    (
        "Congratulations, you've won!",
        "winner@sketchy.example",
        "You have been selected to receive a prize. Click here to claim it "
        "within 24 hours.",
        "spam",
        0.97,
        96,
    ),
    (
        "Server maintenance window Sunday",
        "support@aws.amazon.com",
        "We'll be performing scheduled maintenance on your region Sunday "
        "02:00-04:00 UTC. No action is required.",
        "fyi",
        0.74,
        110,
    ),
    # One unclassified thread, so the "unclassified" bucket isn't mysteriously
    # empty and the null-confidence rendering path gets exercised.
    (
        "Fwd: notes from the offsite",
        "carol@acme.io",
        "Forwarding the raw notes -- I haven't cleaned these up yet.",
        None,
        None,
        130,
    ),
)


def _demo_account(db: Session, user: AppUser) -> ProviderAccount:
    """Find or create the placeholder connection that owns the sample threads.

    The account deliberately has no refresh token: the periodic sync only picks
    up accounts where refresh_token IS NOT NULL, so the scheduler skips this one
    instead of hammering Gmail with a fake credential. Leaving sync_paused_at
    null keeps the UI from flagging it as needing reconnection.

    The lookup matches either this user's new per-user external id or the old
    shared LEGACY_DEMO_EXTERNAL_ID -- a user seeded before that id was scoped
    per user still has a row under the legacy literal, and returning that row
    (rather than missing it and inserting a second one) both avoids a second
    hit on the (provider, external_user_id) constraint and keeps
    seed_demo_data's idempotency guard, which is scoped to whichever account
    this returns, pointed at the same account every time.
    """
    external_id = _demo_external_id(user.id)
    account = (
        db.execute(
            select(ProviderAccount)
            .where(
                ProviderAccount.user_id == user.id,
                ProviderAccount.external_user_id.in_((external_id, LEGACY_DEMO_EXTERNAL_ID)),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if account:
        return account

    account = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id=external_id,
        access_token="demo-seed-no-token",
        refresh_token=None,
        display_email=DEMO_ACCOUNT_EMAIL,
    )
    db.add(account)
    db.flush()
    return account


def seed_demo_data(db: Session, user: AppUser) -> int:
    """Give ``user`` a mailbox of sample threads. Returns the number created.

    Idempotent: if the demo account already owns threads, this does nothing and
    returns 0, so calling it twice can't duplicate the sample inbox.
    """
    account = _demo_account(db, user)

    already = db.execute(
        select(MailThread.id).where(MailThread.provider_account_id == account.id).limit(1)
    ).first()
    if already:
        return 0

    now = datetime.now(timezone.utc)
    created = 0
    for index, (subject, sender, snippet, label, confidence, hours_ago) in enumerate(_SAMPLE):
        sent_at = now - timedelta(hours=hours_ago)
        thread = MailThread(
            user_id=user.id,
            provider_account_id=account.id,
            provider="gmail",
            provider_thread_id=f"demo-thread-{index}-{uuid.uuid4().hex[:8]}",
            subject=subject,
            last_message_at=sent_at,
        )
        db.add(thread)
        db.flush()

        message = MailMessage(
            thread_id=thread.id,
            provider_message_id=f"{thread.provider_thread_id}-m0",
            sender=sender,
            recipient=[user.email],
            sent_at=sent_at,
            snippet=snippet,
            body_text=snippet,
        )
        db.add(message)
        db.flush()

        if label is not None:
            db.add(
                Classification(
                    message_id=message.id,
                    label=label,
                    confidence=confidence,
                    model_version=DEMO_MODEL_VERSION,
                )
            )
        created += 1

    db.commit()
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed sample threads for a user so the console isn't empty.",
    )
    parser.add_argument("email", help="email address of an existing user")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        user = db.execute(
            select(AppUser).where(AppUser.email == args.email.lower())
        ).scalar_one_or_none()
        if user is None:
            print(f"No user with email {args.email!r}. Sign in once first.", file=sys.stderr)
            return 1
        created = seed_demo_data(db, user)

    if created:
        print(f"Seeded {created} sample threads for {args.email}.")
    else:
        print(f"{args.email} already has demo data; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
