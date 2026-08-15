import { useEffect, useRef, useState } from "react";
import { Loader2, Reply as ReplyIcon, Send, Sparkles } from "lucide-react";
import { ApiError, draftReply, sendReply } from "@/lib/api";
import { senderName } from "@/lib/sender";
import type { ReplySent, ThreadMessage } from "@/lib/types";

// Every §3.8 error code the reply-send route can answer with, mapped to
// plain, ultra-clear copy (docs/plans/2026-08-13-reply-plan.md §3.10). Kept
// as our own words rather than echoing the server's `message` verbatim, so
// the composer's copy can't silently drift out of "plain English" even if
// the backend's wording changes.
export const SEND_ERROR_COPY: Record<string, string> = {
  missing_scope: "This account needs to be reconnected to allow sending replies.",
  no_reply_target: "There's no one to reply to in this thread.",
  too_many_recipients: "This reply has too many recipients to send safely.",
  account_identity_unavailable:
    "CortexMail can't confirm this account's own address right now. Reconnect the account and try again.",
  reply_state_stale: "This thread changed since you opened it.",
  reply_thread_busy: "The mailbox is busy syncing — try again in a moment.",
  reply_in_flight: "A reply to this thread is already in progress.",
  reply_superseded: "This reply attempt was replaced by a newer one. Nothing was sent.",
  account_paused: "This account is paused and needs to be reconnected before you can reply.",
  missing_refresh_token: "This account needs to be reconnected before you can reply.",
  send_outcome_unknown:
    "Your reply may have been sent. The thread will update once your mailbox syncs — don't resend unless it's still missing after that.",
  send_rejected: "The reply was rejected and wasn't sent. It's safe to try again.",
  send_unavailable: "Nothing was sent — sending is temporarily unavailable.",
  rate_limited: "Too many replies sent recently. Wait a moment and try again.",
};

// The AI-draft endpoint's own 409/502 codes (plan §3.7) -- distinct from the
// send codes above, and each one disables the "Draft with AI" button with a
// tooltip rather than blocking the composer itself (you can still write the
// reply by hand).
export const DRAFT_ERROR_COPY: Record<string, string> = {
  reply_draft_credential_blocked:
    "Your custom LLM endpoint is currently blocked by policy. Fix or remove it to draft with AI.",
  reply_draft_credential_missing: "No LLM credential is configured for drafting replies.",
  reply_draft_failed: "Couldn't generate a draft reply. You can still write one yourself.",
};

// Codes where the real fix is reconnecting the account -- these get a
// "Reconnect account" action next to the copy instead of just sitting there.
const RECONNECT_CODES = new Set([
  "missing_scope",
  "account_paused",
  "missing_refresh_token",
  "account_identity_unavailable",
]);

// Only the two credential-shaped draft codes mean "this stays broken until
// you fix the credential" -- reply_draft_failed is a one-off failure the
// operator can just retry, so it must not join this set (see handleDraft).
const DRAFT_CREDENTIAL_CODES = new Set([
  "reply_draft_credential_missing",
  "reply_draft_credential_blocked",
]);

// Result of a reply_state_stale refetch (plan §3.9). Distinguishing "the
// refetch itself failed" from "it succeeded and the thread is unchanged"
// matters: collapsing both to a bare replied_at lets a network blip during
// the refetch masquerade as a confirmed fresh state, enabling "Send anyway"
// to resubmit expected_replied_at: null and bypass the fence entirely.
export type ReplyStateRefetch = { ok: true; repliedAt: string | null } | { ok: false };

// Pulls the bare address out of a "Display Name <addr@x.com>" sender string
// (or returns a plain address as-is). Lowercased for self-address exclusion.
function extractAddress(raw: string | null): string | null {
  if (!raw) return null;
  const angle = raw.match(/<([^<>]+)>/);
  const addr = (angle ? angle[1] : raw).trim().toLowerCase();
  return addr || null;
}

// Distinct non-self participants, newest message first -- the closest
// approximation the console can build from what ThreadMessage actually
// carries. NOTE: the real backend's reply-all recipient set also includes
// the original message's To/Cc addresses (plan §3.3), which the console has
// no visibility into (ThreadMessage exposes `sender` only, no recipient/cc
// arrays) -- this is a deliberately narrower, informational-only preview,
// never authoritative (§3.10: "no prepare/preview endpoint").
export function approximateRecipients(
  messages: ThreadMessage[],
  selfAddress: string | null,
): string[] {
  const self = selfAddress?.toLowerCase() ?? null;
  const sorted = [...messages].sort((a, b) => {
    const at = a.sent_at ? Date.parse(a.sent_at) : 0;
    const bt = b.sent_at ? Date.parse(b.sent_at) : 0;
    return bt - at;
  });
  const seen = new Set<string>();
  const names: string[] = [];
  for (const m of sorted) {
    const addr = extractAddress(m.sender);
    if (!addr || addr === self || seen.has(addr)) continue;
    seen.add(addr);
    names.push(senderName(m.sender) ?? addr);
  }
  return names;
}

export function recipientLine(names: string[], replyAll: boolean): string {
  if (names.length === 0) return "Recipients are computed when you send.";
  if (!replyAll || names.length === 1) return `To ${names[0]}`;
  const others = names.length - 1;
  return `To ${names[0]} · +${others} other${others === 1 ? "" : "s"} on reply-all`;
}

interface Props {
  threadId: string;
  provider: string;
  // Seeds `expected_replied_at` (plan §3.9) -- the fetched thread detail's
  // own replied_at, kept live by the parent so a background refresh (or this
  // same composer's own successful send) is reflected without extra plumbing.
  repliedAt: string | null;
  messages: ThreadMessage[];
  selfAddress: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSent: (threadId: string, result: ReplySent) => void;
  // Absent when there's nowhere useful to send a reconnect click (shouldn't
  // happen in practice -- every thread has an owning account -- but the
  // composer degrades to plain copy rather than a dead button).
  onReconnect?: () => void;
  // Re-fetches the thread for the reply_state_stale recovery (plan §3.9):
  // refetch -> user-confirmed resend. Must report whether the refetch itself
  // succeeded (see ReplyStateRefetch) -- a failed refetch is not the same as
  // a successful one that confirms nothing changed.
  onRefetchReplyState: (threadId: string) => Promise<ReplyStateRefetch>;
  // Bumped by the parent on every Shift+R press so the textarea re-focuses
  // even when the composer was already open (plan §3.10).
  focusToken?: number;
}

type Phase = "idle" | "sending" | "drafting";

export function ReplyComposer({
  threadId,
  provider,
  repliedAt,
  messages,
  selfAddress,
  open,
  onOpenChange,
  onSent,
  onReconnect,
  onRefetchReplyState,
  focusToken,
}: Props) {
  const [bodyText, setBodyText] = useState("");
  const [replyAll, setReplyAll] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // reply_in_flight's blocking attempt id (plan §3.6) -- present only while
  // the composer is showing the "abandon and send anyway" override state.
  const [blockerAttemptId, setBlockerAttemptId] = useState<string | null>(null);
  // reply_state_stale recovery state: true once the auto-refetch lands and
  // the composer is waiting on an explicit "send anyway" confirmation.
  const [staleConfirm, setStaleConfirm] = useState(false);
  const [freshRepliedAt, setFreshRepliedAt] = useState<string | null>(null);
  // Persistent: only set for the two credential codes, and only cleared by
  // reconnecting/reconfiguring (there's no retry that would change the
  // outcome). Separate from draftError below, which IS retryable.
  const [draftBlocked, setDraftBlocked] = useState<string | null>(null);
  // reply_draft_failed's one-off message -- clears itself on the next draft
  // attempt, unlike draftBlocked which sticks around until the credential is
  // fixed.
  const [draftError, setDraftError] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Mirrors `phase === "sending"`, but read/written synchronously (see
  // submit()'s single-flight guard below).
  const sendingRef = useRef(false);

  useEffect(() => {
    if (open) textareaRef.current?.focus();
    // focusToken is a deliberate re-run trigger (Shift+R while already
    // open), not something this effect reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, focusToken]);

  // Opening fresh clears whatever error state a previous attempt left behind
  // -- a banner from an old, abandoned send shouldn't greet the next one.
  useEffect(() => {
    if (open) {
      setErrorCode(null);
      setErrorMessage(null);
      setBlockerAttemptId(null);
      setStaleConfirm(false);
      setDraftError(null);
    }
  }, [open]);

  const names = approximateRecipients(messages, selfAddress);

  async function handleSendError(err: ApiError) {
    const code = err.code ?? null;
    if (code === "reply_state_stale") {
      setErrorCode(code);
      setErrorMessage(SEND_ERROR_COPY[code]);
      const refetch = await onRefetchReplyState(threadId);
      // A failed refetch must not enable "Send anyway": that would resubmit
      // expected_replied_at: null, which bypasses the fence entirely on what
      // might be nothing more than a network blip. Only a *successful*
      // refetch gets to offer the confirm-and-resend banner.
      if (refetch.ok) {
        setFreshRepliedAt(refetch.repliedAt);
        setStaleConfirm(true);
      } else {
        setErrorCode(null);
        setErrorMessage(
          "This thread changed since you opened it, and CortexMail couldn't refresh it to confirm. Check your connection and try sending again.",
        );
      }
      return;
    }
    if (code === "reply_in_flight") {
      const attemptId =
        typeof err.detail?.attempt_id === "string" ? err.detail.attempt_id : null;
      setBlockerAttemptId(attemptId);
      setErrorCode(code);
      setErrorMessage(SEND_ERROR_COPY[code]);
      return;
    }
    setErrorCode(code);
    setErrorMessage((code && SEND_ERROR_COPY[code]) || err.message);
  }

  async function submit(overrideAttemptId?: string, expectedRepliedAtOverride?: string | null) {
    // Single-flight, checked against a ref rather than the `phase` state: two
    // synchronous triggers (e.g. a stray double Ctrl+Enter) can both run
    // before React re-renders with "sending", so a state read here would
    // still see the stale "idle" value for the second call. The ref updates
    // immediately, so the second call is guaranteed to see the first one.
    if (sendingRef.current) return;
    const trimmed = bodyText.trim();
    if (!trimmed) return;
    sendingRef.current = true;
    setPhase("sending");
    setErrorCode(null);
    setErrorMessage(null);
    // Recovery banners (stale-confirm, in-flight-blocker) are only cleared
    // on success today -- so a "Send anyway"/"Abandon and send anyway" retry
    // that fails with a *different* code (rate_limited, send_rejected, ...)
    // left the old recovery prompt on screen with no visible new error. Every
    // fresh attempt starts clean; handleSendError re-arms whichever banner
    // still applies.
    setStaleConfirm(false);
    setBlockerAttemptId(null);
    try {
      const result = await sendReply(threadId, {
        bodyText: trimmed,
        replyAll,
        expectedRepliedAt:
          expectedRepliedAtOverride !== undefined ? expectedRepliedAtOverride : repliedAt,
        overrideAttemptId: overrideAttemptId ?? null,
      });
      setBodyText("");
      setBlockerAttemptId(null);
      setStaleConfirm(false);
      sendingRef.current = false;
      setPhase("idle");
      onOpenChange(false);
      onSent(threadId, result);
    } catch (e) {
      sendingRef.current = false;
      setPhase("idle");
      if (e instanceof ApiError) {
        await handleSendError(e);
      } else {
        setErrorCode(null);
        setErrorMessage((e as Error).message || "Something went wrong sending this reply.");
      }
    }
  }

  async function handleDraft() {
    if (phase !== "idle" || draftBlocked) return;
    setPhase("drafting");
    setDraftError(null);
    try {
      const result = await draftReply(threadId);
      // A deliberate click on "Draft with AI" is a fresh-start action --
      // it replaces whatever's in the box rather than trying to merge.
      setBodyText(result.draft_text);
      setPhase("idle");
      textareaRef.current?.focus();
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError && e.code && DRAFT_CREDENTIAL_CODES.has(e.code)) {
        // Credential codes stay broken until the operator fixes the
        // credential -- disabling the button (rather than just showing an
        // error) is the right call here.
        setDraftBlocked(DRAFT_ERROR_COPY[e.code]);
      } else if (e instanceof ApiError && e.code && DRAFT_ERROR_COPY[e.code]) {
        // reply_draft_failed and friends: a one-off failure, not a broken
        // credential -- the button stays enabled so the operator can just
        // try again.
        setDraftError(DRAFT_ERROR_COPY[e.code]);
      } else {
        setErrorCode(null);
        setErrorMessage(
          (e as Error).message || "Couldn't generate a draft reply. You can still write one yourself.",
        );
      }
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void submit();
    }
  }

  if (!open) {
    // The pane's headline action, so it wears the primary tint (the
    // backfill/run-ingest treatment) instead of the muted ghost styling the
    // utility controls use -- solid primary stays reserved for the actual
    // send button once the composer is open.
    return (
      <div className="shrink-0 border-t border-border p-2 flex items-center justify-end gap-2.5">
        <span
          aria-hidden="true"
          className="hidden md:inline-flex items-center gap-1 text-[10.5px] font-mono text-muted-foreground"
        >
          <span className="kbd">shift</span>
          <span className="kbd">r</span>
        </span>
        <button
          type="button"
          onClick={() => onOpenChange(true)}
          title="Reply ( Shift R )"
          className="inline-flex items-center gap-1.5 h-8 max-md:h-10 px-3.5 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary-tint-foreground font-mono text-[12px] font-medium cursor-pointer transition-colors"
        >
          <ReplyIcon className="h-3.5 w-3.5" />
          Reply
        </button>
      </div>
    );
  }

  const sending = phase === "sending";
  const showReconnect = !!errorCode && RECONNECT_CODES.has(errorCode) && !!onReconnect;

  return (
    <div className="shrink-0 border-t border-border bg-[var(--color-panel)] p-3 space-y-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div role="group" aria-label="reply mode" className="flex items-center gap-1">
          <button
            type="button"
            aria-pressed={!replyAll}
            onClick={() => setReplyAll(false)}
            className={[
              "h-6 px-2 rounded border font-mono text-[11px] cursor-pointer transition-colors",
              !replyAll
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            Reply
          </button>
          <button
            type="button"
            aria-pressed={replyAll}
            onClick={() => setReplyAll(true)}
            className={[
              "h-6 px-2 rounded border font-mono text-[11px] cursor-pointer transition-colors",
              replyAll
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            Reply all
          </button>
        </div>
        {/* Informational only -- computed for real, server-side, at send
            time (plan §3.3/§3.10). Never treat this line as authoritative. */}
        <span
          title="Approximate — the real recipients are computed when you send."
          className="text-[11px] text-muted-foreground font-mono truncate max-w-[60%]"
        >
          {recipientLine(names, replyAll)}
        </span>
      </div>

      {staleConfirm ? (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 flex items-center justify-between gap-2 flex-wrap">
          <span className="text-[11px] font-mono text-foreground/85">
            A reply was just sent from another window — send anyway?
          </span>
          <button
            type="button"
            onClick={() => void submit(undefined, freshRepliedAt)}
            className="h-6 px-2 rounded border border-amber-600/50 font-mono text-[11px] text-foreground hover:bg-amber-500/10 cursor-pointer transition-colors shrink-0"
          >
            Send anyway
          </button>
        </div>
      ) : blockerAttemptId ? (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 flex items-center justify-between gap-2 flex-wrap">
          <span className="text-[11px] font-mono text-foreground/85">
            {SEND_ERROR_COPY.reply_in_flight}
          </span>
          <button
            type="button"
            onClick={() => void submit(blockerAttemptId)}
            className="h-6 px-2 rounded border border-amber-600/50 font-mono text-[11px] text-foreground hover:bg-amber-500/10 cursor-pointer transition-colors shrink-0"
          >
            Abandon and send anyway
          </button>
        </div>
      ) : (
        errorMessage && (
          <div
            role="alert"
            className="rounded border border-destructive/40 bg-destructive/10 px-2.5 py-2 flex items-center justify-between gap-2 flex-wrap"
          >
            <span className="text-[11px] font-mono text-destructive">{errorMessage}</span>
            {showReconnect && (
              <button
                type="button"
                onClick={onReconnect}
                className="h-6 px-2 rounded border border-destructive/50 font-mono text-[11px] text-foreground hover:bg-destructive/10 cursor-pointer transition-colors shrink-0"
              >
                Reconnect account
              </button>
            )}
          </div>
        )
      )}

      <textarea
        ref={textareaRef}
        value={bodyText}
        onChange={(e) => setBodyText(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={sending}
        placeholder="Write a reply… (Ctrl+Enter to send)"
        rows={5}
        className="w-full resize-y rounded border border-border bg-background px-2.5 py-2 font-sans text-[13px] leading-relaxed text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-60"
      />

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => void handleDraft()}
          disabled={phase !== "idle" || !!draftBlocked}
          title={draftBlocked ?? undefined}
          className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded border border-border font-mono text-[12px] text-muted-foreground hover:text-foreground hover:border-foreground/30 cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:text-muted-foreground disabled:hover:border-border"
        >
          {phase === "drafting" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Sparkles className="h-3.5 w-3.5" />
          )}
          Draft with AI
        </button>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="h-8 px-3 rounded font-mono text-[12px] text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={sending || !bodyText.trim()}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded border border-primary bg-primary text-primary-foreground font-mono text-[12px] font-medium cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {sending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Sending…
              </>
            ) : (
              <>
                <Send className="h-3.5 w-3.5" />
                Send
              </>
            )}
          </button>
        </div>
      </div>
      {draftError && (
        <p role="alert" className="text-[11px] font-mono text-destructive">
          {draftError}
        </p>
      )}
      {provider === "outlook" && (
        <p className="text-[10.5px] font-mono text-muted-foreground">
          Sent replies appear in this thread once your mailbox syncs.
        </p>
      )}
    </div>
  );
}
