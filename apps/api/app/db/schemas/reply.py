from datetime import datetime
from uuid import UUID

from .common import Response


class MessageOut(Response):
    """The sent reply, shaped like a thread-detail message (see
    ``schemas/mailbox.py``'s ``ThreadMessage``) plus the recipients W1's
    provider builders already compute -- ``recipient``/``cc`` let the
    composer confirm who the reply actually went to without a second call.

    For Outlook, ``id`` is the ``reply_attempt`` row's own id, NOT a
    ``MailMessage.id`` (plan §3.4/P7-3 -- nothing is written to
    ``mail_message`` for Outlook at send time). It's client-display-only:
    no server mutation endpoint ever accepts it back. The real message row
    arrives later via correlated sentitems ingest.
    """

    id: UUID
    sent_at: datetime | None
    sender: str | None
    recipient: list[str] | None
    cc: list[str] | None
    snippet: str | None
    body_text: str | None
    body_html: str | None


class ReplySent(Response):
    thread_id: UUID
    message: MessageOut
    # The reply fence, advanced by this send (plan §3.5) -- always non-null
    # by the time a 200 is returned, since a successful send always
    # advances it.
    replied_at: datetime
    resolved_action_items: int


class ReplyDraft(Response):
    draft_text: str
    provider: str
    model: str
