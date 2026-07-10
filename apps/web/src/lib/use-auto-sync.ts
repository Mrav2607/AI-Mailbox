import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ApiError, ingestGmail, waitForTask } from "./api";

// Cadence presets for the UI; loadUi accepts any non-negative number though,
// so a hand-edited (or test-written) blob can run faster or slower.
export const AUTO_SYNC_CHOICES: { value: number; label: string }[] = [
  { value: 0, label: "off" },
  { value: 60, label: "1m" },
  { value: 180, label: "3m" },
  { value: 600, label: "10m" },
];

interface AutoSyncOptions {
  intervalSec: number; // 0 disables
  enabled: boolean; // false while logged out
  busy: boolean; // a manual ingest/backfill is running — stay out of its way
  onSessionExpired: () => void;
}

/*
  Background mail sync. Every `intervalSec` seconds this quietly queues the
  same deduping Gmail ingest the toolbar button uses and accumulates the
  worker-reported count of genuinely new threads into `pendingNew` — the
  number behind the "N new · refresh" pill. The list itself is never touched;
  the operator decides when to refresh.

  Runs are chained (next scheduled only after the previous finishes), so a
  slow worker can never stack two ingests. The timer pauses while the tab is
  hidden and catches up on return. Failures stay quiet: a `syncFailed` flag
  for a subtle indicator, one toast if three runs fail in a row.
*/
export function useAutoSync({
  intervalSec,
  enabled,
  busy,
  onSessionExpired,
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
  // Mirrored into refs so the loop effect only depends on [enabled, interval]
  // and a busy-flag flip doesn't reset the countdown.
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const onSessionExpiredRef = useRef(onSessionExpired);
  onSessionExpiredRef.current = onSessionExpired;

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
        const r = await ingestGmail(25, true, false);
        let newCount: number;
        if (r.task_id) {
          const final = await waitForTask(r.task_id, {
            intervalMs: 2000,
            timeoutMs: 90_000,
          });
          if (final.result?.status === "error" || final.error) {
            throw new Error(final.result?.detail ?? final.error ?? "sync failed");
          }
          // Timed out still-running: neutral — the threads land eventually and
          // the next refresh (manual or auto) picks them up.
          if (!final.ready) return;
          newCount = final.result?.threads_upserted ?? 0;
        } else {
          newCount = r.new_threads ?? 0;
        }
        failStreakRef.current = 0;
        setSyncFailed(false);
        if (newCount > 0) setPendingNew((p) => p + newCount);
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
  }, [enabled, intervalSec]);

  const clearNew = useCallback(() => setPendingNew(0), []);

  return { pendingNew, clearNew, syncFailed };
}
