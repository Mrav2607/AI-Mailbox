import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ApiError,
  getTriage,
  ingestGmail,
  waitForTask,
  WORKER_TASK_TIMEOUT_MS,
} from "./api";

// Cadence presets for the UI; loadUi accepts any non-negative number though,
// so a hand-edited (or test-written) blob can run faster or slower.
export const AUTO_SYNC_CHOICES: { value: number; label: string }[] = [
  { value: 0, label: "off" },
  { value: 60, label: "1m" },
  { value: 180, label: "3m" },
  { value: 600, label: "10m" },
];

// How many of the newest threads the new-mail check scans; the pill caps at
// this many ("50+").
export const NEW_MAIL_SCAN_LIMIT = 50;

// Per-user "newest mail I've acknowledged" watermark. Persisted so the pill
// survives reloads: it's derived by comparing server data against this mark,
// never by catching a one-shot task result that a reload could orphan.
const seenKey = (userId: string) => `ai_mailbox_seen:${userId}`;

function readSeenMs(userId: string): number {
  try {
    const raw = window.localStorage.getItem(seenKey(userId));
    const ms = raw ? Date.parse(raw) : NaN;
    return Number.isFinite(ms) ? ms : 0;
  } catch {
    return 0;
  }
}

function writeSeen(userId: string, iso: string) {
  try {
    window.localStorage.setItem(seenKey(userId), iso);
  } catch {
    /* storage unavailable; the pill just won't survive reloads */
  }
}

interface AutoSyncOptions {
  intervalSec: number; // 0 disables
  // False while logged out — and while the mailbox is empty: a new-only pull
  // has no baseline thread to anchor against, so auto-sync waits for the
  // first manual ingest.
  enabled: boolean;
  busy: boolean; // a manual ingest/backfill is running — stay out of its way
  userId: string | null; // scopes the acknowledged-mail watermark
  onSessionExpired: () => void;
  // Fires after any sync that changed the DB (new mail OR backfilled
  // history), so the whole console — list, sidebar counts, overview stats —
  // can quietly track reality without the operator touching anything.
  onSynced: () => void | Promise<void>;
}

/*
  Background mail sync. Every `intervalSec` seconds this quietly queues a
  new-only Gmail pull — threads that arrived after the newest one already in
  the DB, never older backfill — then re-derives the "N new" pill: open
  threads whose last_message_at is newer than the persisted acknowledged
  watermark. Deriving (rather than accumulating worker-reported counts) makes
  the pill reload-proof — a sync that completes while the tab is gone still
  surfaces on the next mount's check.

  Runs are chained (next scheduled only after the previous finishes), so a
  slow worker can never stack two ingests. The timer pauses while the tab is
  hidden and catches up on return. Failures stay quiet: a `syncFailed` flag
  for a subtle indicator, one toast if three runs fail in a row.
*/
export function useAutoSync({
  intervalSec,
  enabled,
  busy,
  userId,
  onSessionExpired,
  onSynced,
}: AutoSyncOptions): {
  pendingNew: number;
  clearNew: () => void;
  syncFailed: boolean;
} {
  const [pendingNew, setPendingNew] = useState(0);
  const [syncFailed, setSyncFailed] = useState(false);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nextDueRef = useRef(0);
  const runningRef = useRef(false);
  const failStreakRef = useRef(0);
  // Newest last_message_at the check has observed (server timestamp, so the
  // watermark never depends on the client clock once data exists).
  const newestRef = useRef<string | null>(null);
  // Mirrored into refs so the loop effect only depends on [enabled, interval]
  // and a busy-flag flip doesn't reset the countdown.
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const userIdRef = useRef(userId);
  userIdRef.current = userId;
  const onSessionExpiredRef = useRef(onSessionExpired);
  onSessionExpiredRef.current = onSessionExpired;
  const onSyncedRef = useRef(onSynced);
  onSyncedRef.current = onSynced;

  // Re-derive pendingNew from the newest open threads. With `acknowledge`,
  // instead mark everything currently on the server as seen.
  const checkNew = useCallback(async (acknowledge = false) => {
    const uid = userIdRef.current;
    if (!uid) return;
    try {
      const res = await getTriage("all", NEW_MAIL_SCAN_LIMIT);
      let newestIso: string | null = null;
      let newestMs = 0;
      const itemMs: number[] = [];
      for (const it of res.items) {
        const ms = it.last_message_at ? Date.parse(it.last_message_at) : NaN;
        if (!Number.isFinite(ms)) continue;
        itemMs.push(ms);
        if (ms > newestMs) {
          newestMs = ms;
          newestIso = it.last_message_at;
        }
      }
      newestRef.current = newestIso;
      const seenMs = readSeenMs(uid);
      // First visit (or acknowledging): everything currently there is "seen".
      if (acknowledge || !seenMs) {
        writeSeen(uid, newestIso ?? new Date().toISOString());
        setPendingNew(0);
        return;
      }
      setPendingNew(itemMs.filter((ms) => ms > seenMs).length);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        onSessionExpiredRef.current();
        return;
      }
      if (acknowledge) {
        // Still honor the dismissal even if the fetch died.
        writeSeen(uid, newestRef.current ?? new Date().toISOString());
        setPendingNew(0);
      }
    }
  }, []);

  const clearNew = useCallback(() => {
    // Optimistically drop the pill, then persist the acknowledgment against
    // fresh server data so anything even newer immediately re-counts.
    setPendingNew(0);
    void checkNew(true);
  }, [checkNew]);

  // Mount / login: derive the pill from persisted state so mail that landed
  // while this tab wasn't looking (reload mid-sync, closed tab) still shows.
  useEffect(() => {
    if (!enabled || !userId) return;
    void checkNew();
  }, [enabled, userId, checkNew]);

  useEffect(() => {
    if (!enabled || intervalSec <= 0) return;

    const schedule = (delayMs: number) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      nextDueRef.current = Date.now() + delayMs;
      timerRef.current = setTimeout(tick, delayMs);
    };

    const runOnce = async () => {
      runningRef.current = true;
      try {
        // new-only: pull everything that arrived after the newest known
        // thread and nothing else — background cycles never backfill.
        const r = await ingestGmail(100, true, false, true);
        let changed: boolean;
        if (r.task_id) {
          const final = await waitForTask(r.task_id, {
            intervalMs: 2000,
            timeoutMs: WORKER_TASK_TIMEOUT_MS,
          });
          if (final.result?.status === "error" || final.error) {
            throw new Error(final.result?.detail ?? final.error ?? "sync failed");
          }
          // The timeout extends beyond the worker's hard execution limit. If
          // the broker itself is unavailable for longer, treat the outcome as
          // unknown: refresh whatever landed and let the check re-derive.
          changed = !final.ready || (final.result?.threads_upserted ?? 0) > 0;
        } else {
          changed = (r.new_threads ?? 0) > 0;
        }
        failStreakRef.current = 0;
        setSyncFailed(false);
        if (changed) await onSyncedRef.current();
        // Always re-derive: mail can also land via another tab's manual
        // ingest, and the check is one cheap unthrottled GET.
        await checkNew();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          onSessionExpiredRef.current();
          return;
        }
        // Rate-limited just means a manual run got there first this window.
        if (e instanceof ApiError && e.status === 429) return;
        failStreakRef.current += 1;
        setSyncFailed(true);
        if (failStreakRef.current === 3) {
          toast.error("auto-sync failing — retrying in the background");
        }
      } finally {
        runningRef.current = false;
      }
    };

    const tick = () => {
      // Hidden tab: stop here; the visibility handler resumes on return.
      if (document.hidden) return;
      if (busyRef.current || runningRef.current) {
        schedule(15_000);
        return;
      }
      void runOnce().finally(() => schedule(intervalSec * 1000));
    };

    const onVisibility = () => {
      if (document.hidden) {
        // Keep nextDue so the return handler knows whether we're overdue.
        if (timerRef.current) clearTimeout(timerRef.current);
        return;
      }
      const remaining = nextDueRef.current - Date.now();
      if (remaining <= 0) tick();
      else schedule(remaining);
    };

    document.addEventListener("visibilitychange", onVisibility);
    // First run a full interval out — mount already loads fresh data.
    schedule(intervalSec * 1000);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [enabled, intervalSec, checkNew]);

  return { pendingNew, clearNew, syncFailed };
}
