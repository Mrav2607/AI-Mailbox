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
const seenKey = (userId: string) => `ai_mailbox_seen:${userId}`;
const channelName = (userId: string) => `ai-mailbox-sync:${userId}`;

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
          `ai-mailbox-auto-sync:${userId}`,
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
        const anySucceeded = settled.some(
          (s) => s.status === "fulfilled" && s.value.status === "succeeded",
        );
        if (!cancelled && anySucceeded) {
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
  }, [enabled, intervalSec, checkNew, leader]);

  return { pendingNew, clearNew, syncFailed, health };
}
