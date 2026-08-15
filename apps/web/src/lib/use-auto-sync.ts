import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ApiError,
  getActiveSync,
  getSyncHealth,
  getTriage,
  ingestMail,
  waitForSyncRuns,
  type SyncHealth,
  type SyncRunStatus,
} from "./api";
import { leftUnclassifiedMessage } from "./task-toasts";

// The server pulls mail on its own schedule now, so this only has to be often
// enough that the user notices a broken mailbox in reasonable time
const HEALTH_POLL_MS = 60_000;

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
const seenKey = (userId: string) => `cortexmail_seen:${userId}`;
const channelName = (userId: string) => `cortexmail-sync:${userId}`;

export function broadcastSyncComplete(userId: string) {
  if (!("BroadcastChannel" in window)) return;
  const channel = new BroadcastChannel(channelName(userId));
  channel.postMessage({ type: "sync-complete" });
  channel.close();
}

function useSyncLeader(enabled: boolean, userId: string | null): boolean {
  const [leader, setLeader] = useState(false);

  useEffect(() => {
    if (!enabled || !userId) {
      setLeader(false);
      return;
    }
    if (!("locks" in navigator)) {
      // No Web Locks (insecure context, e.g. plain-http LAN access): treat
      // visibility as leadership so hiding the tab still pauses-and-resumes
      // the loop. Multiple visible tabs may all sync; backend single-flight
      // dedupes them.
      const sync = () => setLeader(!document.hidden);
      sync();
      document.addEventListener("visibilitychange", sync);
      return () => {
        document.removeEventListener("visibilitychange", sync);
        setLeader(false);
      };
    }

    let cancelled = false;
    let acquiring = false;
    let release: (() => void) | null = null;
    let lockController: AbortController | null = null;
    const acquire = () => {
      if (cancelled || acquiring || document.hidden) return;
      acquiring = true;
      const controller = new AbortController();
      lockController = controller;
      void navigator.locks
        .request(
          `cortexmail-auto-sync:${userId}`,
          { signal: controller.signal },
          async () => {
            acquiring = false;
            if (cancelled) return;
            setLeader(true);
            await new Promise<void>((resolve) => {
              release = resolve;
            });
            release = null;
            setLeader(false);
          },
        )
        .catch(() => {
          acquiring = false;
          // Abort is expected when a queued/visible tab becomes hidden. Other
          // lock failures fall back to backend single-flight correctness.
          if (!cancelled && !document.hidden) {
            if (controller.signal.aborted) acquire();
            else setLeader(true);
          }
        });
    };
    const onVisibility = () => {
      if (document.hidden) {
        lockController?.abort();
        release?.();
        setLeader(false);
      } else {
        acquire();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    acquire();
    return () => {
      cancelled = true;
      lockController?.abort();
      release?.();
      setLeader(false);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, userId]);

  return leader;
}

// Pure warn/dedupe/reset logic behind the "N left unclassified" toast
// (docs/plans/2026-08-14-llm-failure-visibility-plan.md phase 2) -- kept
// standalone (and exported) so this semantics can be unit tested directly,
// without mounting the hook's leader-election/broadcast-channel/timer
// machinery. `alreadyWarned` is the caller's current streak flag; the
// return says both what to do THIS run and what the flag should become.
export function nextLeftUnclassifiedWarning(
  finals: SyncRunStatus[],
  alreadyWarned: boolean,
): { total: number; shouldWarn: boolean; warned: boolean } {
  const total = finals.reduce((sum, f) => sum + (f.result?.left_unclassified ?? 0), 0);
  if (total > 0) return { total, shouldWarn: !alreadyWarned, warned: true };
  return { total, shouldWarn: false, warned: false };
}

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
  health: SyncHealth | null;
} {
  const [pendingNew, setPendingNew] = useState(0);
  const [syncFailed, setSyncFailed] = useState(false);
  const [health, setHealth] = useState<SyncHealth | null>(null);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runningRef = useRef(false);
  const failStreakRef = useRef(0);
  // True once this loop has already warned about mail left unclassified --
  // reset the moment a run comes back clean, so a provider that recovers
  // and fails again gets warned about it again instead of staying silent
  // forever after the first toast.
  const leftUnclassifiedWarnedRef = useRef(false);
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
  const leader = useSyncLeader(enabled && intervalSec > 0, userId);
  const channelRef = useRef<BroadcastChannel | null>(null);

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

  // Server truth about whether mail is actually flowing. Polled on its own slow
  // cadence and independent of `enabled`.
  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const next = await getSyncHealth(controller.signal);
        if (!cancelled) setHealth(next);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onSessionExpiredRef.current();
        // Otherwise stay quiet: a failed health check is not itself news.
      }
    };

    void poll();
    const timer = setInterval(() => {
      if (!document.hidden) void poll();
    }, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, [userId]);

  useEffect(() => {
    if (!enabled || !userId || !("BroadcastChannel" in window)) return;
    const channel = new BroadcastChannel(channelName(userId));
    channelRef.current = channel;
    channel.onmessage = (event) => {
      if (event.data?.type === "sync-complete") {
        void Promise.all([onSyncedRef.current(), checkNew()]);
      } else if (event.data?.type === "acknowledged") {
        void checkNew();
      }
    };
    return () => {
      channelRef.current = null;
      channel.close();
    };
  }, [enabled, userId, checkNew]);

  // Console keeps this hook mounted across logout/login, so without this a
  // `warned: true` left over from the PREVIOUS account's session would
  // silently suppress the next account's first real warning -- same class
  // of per-account leak use-llm-panel.ts's own `[userId]` reset effect
  // guards against for its state. No race with a late settle from the old
  // account: the cadence effect below aborts anything it left in flight
  // (its own cleanup, keyed on `enabled`) before a NEW instance starts for
  // the next account -- this app never swaps directly from one logged-in
  // user to another, always through a logged-out (`enabled: false`)
  // moment in between, so that abort always lands before this reset could
  // be clobbered by a stale continuation.
  useEffect(() => {
    leftUnclassifiedWarnedRef.current = false;
  }, [userId]);

  // Thin wrapper around nextLeftUnclassifiedWarning that owns this hook's
  // streak ref -- shared by the normal completion path below AND the
  // reload/reattach path, so a tab reloaded mid-sync still gets told its
  // mail went unclassified instead of the warning only firing for a run
  // that happened to finish while the tab stayed open.
  const warnLeftUnclassified = useCallback((finals: SyncRunStatus[]) => {
    const { total, shouldWarn, warned } = nextLeftUnclassifiedWarning(
      finals,
      leftUnclassifiedWarnedRef.current,
    );
    leftUnclassifiedWarnedRef.current = warned;
    if (shouldWarn) toast.warning(leftUnclassifiedMessage(total));
  }, []);

  const clearNew = useCallback(() => {
    // Optimistically drop the pill, then persist the acknowledgment against
    // fresh server data so anything even newer immediately re-counts.
    setPendingNew(0);
    void checkNew(true);
    channelRef.current?.postMessage({ type: "acknowledged" });
  }, [checkNew]);

  // Mount / login: derive the pill from persisted state so mail that landed
  // while this tab wasn't looking (reload mid-sync, closed tab) still shows.
  useEffect(() => {
    if (!enabled || !userId) return;
    void checkNew();
  }, [enabled, userId, checkNew]);

  useEffect(() => {
    if (!enabled || intervalSec <= 0 || !leader) return;

    let cancelled = false;
    const controller = new AbortController();

    const schedule = (delayMs: number) => {
      if (cancelled) return;
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(tick, delayMs);
    };

    const runOnce = async () => {
      runningRef.current = true;
      try {
        // new-only: pull everything that arrived after the newest known
        // thread and nothing else — background cycles never backfill. One
        // run per connected account; nothing connected means nothing to do.
        const queued = await ingestMail(100, true, false, true);
        if (queued.length === 0) {
          failStreakRef.current = 0;
          setSyncFailed(false);
          return;
        }
        const settled = await waitForSyncRuns(queued, { signal: controller.signal });
        const finals: SyncRunStatus[] = [];
        for (const s of settled) {
          if (s.status === "fulfilled") finals.push(s.value);
          else throw s.reason;
        }
        for (const f of finals) {
          if (f.status === "failed" || f.result?.status === "error") {
            throw new Error(f.result?.detail ?? f.error ?? "sync failed");
          }
        }
        const changed =
          finals.some((f) => !f.ready) ||
          finals.some((f) => (f.result?.threads_upserted ?? 0) > 0);
        if (cancelled) return;
        failStreakRef.current = 0;
        setSyncFailed(false);
        if (changed) await onSyncedRef.current();
        // This is otherwise a silent loop, but mail going unclassified is
        // exactly the thing this feature exists to surface.
        warnLeftUnclassified(finals);
        // Always re-derive: mail can also land via another tab's manual
        // ingest, and the check is one cheap unthrottled GET.
        await checkNew();
        channelRef.current?.postMessage({ type: "sync-complete" });
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
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
      void runOnce().finally(() => {
        if (!cancelled) schedule(intervalSec * 1000);
      });
    };

    // Reattach to work orphaned by a reload before starting a new cadence —
    // one run per account may have been in flight when the tab went away.
    void getActiveSync(controller.signal)
      .then(async (active) => {
        if (active.length === 0 || cancelled) return;
        const settled = await waitForSyncRuns(active, { signal: controller.signal });
        const finals = settled
          .filter((s): s is PromiseFulfilledResult<SyncRunStatus> => s.status === "fulfilled")
          .map((s) => s.value);
        if (cancelled) return;
        // A run that finished while this tab was reloading carries the same
        // result payload as one that finished with the tab open -- without
        // this, reloading mid-sync silently drops the warning entirely.
        warnLeftUnclassified(finals);
        const anySucceeded = finals.some((f) => f.status === "succeeded");
        if (anySucceeded) {
          await Promise.all([onSyncedRef.current(), checkNew()]);
          channelRef.current?.postMessage({ type: "sync-complete" });
        }
      })
      .catch((e) => {
        if (!(e instanceof DOMException && e.name === "AbortError")) setSyncFailed(true);
      })
      .finally(() => schedule(intervalSec * 1000));
    return () => {
      cancelled = true;
      controller.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [enabled, intervalSec, checkNew, leader, warnLeftUnclassified]);

  return { pendingNew, clearNew, syncFailed, health };
}
