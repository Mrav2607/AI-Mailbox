import { useState } from "react";
import { cn } from "@/lib/utils";
import { ApiError, updateConnection } from "@/lib/api";
import type { Connection } from "@/lib/types";

// ENABLE-time failure copy (docs/plans/2026-08-13-label-sync-plan.md §3.3),
// mirrored from the API's own routes/auth.py:_ENABLE_FAILURE_MESSAGES so
// this row and the backend read the same words. label_sync_busy's wording
// is frozen verbatim by the plan.
export const LABEL_SYNC_ERROR_COPY: Record<string, string> = {
  missing_scope: "This account needs to be reconnected to grant label-sync permission.",
  reauth_required: "This account needs to be reconnected before label sync can turn on.",
  account_paused:
    "This account is paused and needs to be reconnected before label sync can turn on.",
  label_sync_busy: "a sync is finishing — try again in a few minutes",
};

// Same shape as ReplyComposer's RECONNECT_CODES -- these three are only
// fixable by reconnecting. label_sync_busy is transient (a task's lease
// expires on its own) and gets no reconnect action, just the wait-and-retry
// copy above; the toggle itself stays off either way (plan §3.3 -- ENABLE
// never changes the flag on failure).
export const LABEL_SYNC_RECONNECT_CODES = new Set([
  "missing_scope",
  "reauth_required",
  "account_paused",
]);

/**
 * One connected account's label-sync toggle, drift line, and error/reconnect
 * copy -- rendered under an account row (the popover today, the settings
 * dialog's accounts tab going forward).
 *
 * No local optimistic state: `enabled`/`drift` render straight off the
 * `connection` prop. A successful PATCH calls `onUpdated` with the server's
 * response instead of caching it here -- the caller (App) merges that into
 * its own `connections` state, which is what makes the popover status line
 * and this toggle agree without waiting on the next poll (settings-card plan
 * §3.2/P1-1). Busy/error state stays local to this row; a failed PATCH never
 * touches the parent.
 */
export function LabelSyncRow({
  connection,
  onConnectGmail,
  onConnectOutlook,
  onUpdated,
  disabled,
}: {
  connection: Connection;
  onConnectGmail: () => void;
  onConnectOutlook?: () => void;
  onUpdated: (connection: Connection) => void;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const enabled = connection.label_sync_enabled;
  const drift = connection.label_sync_drift;

  async function handleToggle() {
    if (busy || disabled) return;
    setBusy(true);
    setErrorCode(null);
    setErrorMessage(null);
    try {
      const updated = await updateConnection(connection.id, {
        label_sync_enabled: !enabled,
      });
      onUpdated(updated);
    } catch (e) {
      if (e instanceof ApiError && e.code && LABEL_SYNC_ERROR_COPY[e.code]) {
        setErrorCode(e.code);
        setErrorMessage(LABEL_SYNC_ERROR_COPY[e.code]);
      } else {
        setErrorMessage((e as Error).message || "Couldn't update label sync for this account.");
      }
    } finally {
      setBusy(false);
    }
  }

  const reconnect = connection.provider === "outlook" ? onConnectOutlook : onConnectGmail;
  const showReconnect = !!errorCode && LABEL_SYNC_RECONNECT_CODES.has(errorCode) && !!reconnect;

  return (
    <div className="pl-3.5 space-y-1">
      <label
        className={cn(
          "flex items-center gap-2 text-[11px] font-mono text-foreground/80",
          busy || disabled ? "cursor-not-allowed opacity-70" : "cursor-pointer",
        )}
      >
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy || disabled}
          onChange={() => void handleToggle()}
          className="accent-primary"
        />
        Sync labels to {connection.provider === "outlook" ? "Outlook" : "Gmail"}
      </label>
      <p className="text-[10px] font-mono text-muted-foreground leading-snug">
        creates sorting labels in your mailbox
      </p>
      {enabled && !!drift && (
        <p className="text-[10px] font-mono text-muted-foreground leading-snug">
          syncing — {drift} remaining
        </p>
      )}
      {errorMessage && (
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <span className="text-[10px] font-mono text-destructive leading-snug">
            {errorMessage}
          </span>
          {showReconnect && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                reconnect?.();
              }}
              className="shrink-0 h-5 px-1.5 rounded border border-destructive/50 font-mono text-[10px] text-foreground hover:bg-destructive/10 cursor-pointer transition-colors"
            >
              reconnect
            </button>
          )}
        </div>
      )}
    </div>
  );
}
