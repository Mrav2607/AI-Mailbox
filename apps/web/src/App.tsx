import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  classifyBackfill,
  classifyQueue,
  deleteThread,
  flushDeleteThread,
  getCounts,
  getMe,
  googleAuthCallback,
  getOverview,
  getThread,
  getTriage,
  ingestGmail,
  reclassify,
  searchThreads,
  setThreadDone,
  setToken,
  waitForTask,
  waitForSyncRun,
  WORKER_TASK_TIMEOUT_MS,
} from "@/lib/api";
import type { TaskResult } from "@/lib/api";
import { BUCKET_KEYS } from "@/lib/labels";
import { gmailThreadUrl } from "@/lib/utils";
import type {
  BackfillOptions,
  BucketKey,
  IngestOptions,
  Label,
  Overview,
  ThreadDetail,
  TriageItem,
  User,
} from "@/lib/types";
import { ALL_LABELS } from "@/lib/types";
import { toast } from "sonner";

import { BucketSidebar } from "@/components/console/BucketSidebar";
import { ThreadList } from "@/components/console/ThreadList";
import { ThreadDetailPane } from "@/components/console/ThreadDetailPane";
import { TopBar } from "@/components/console/TopBar";
import { CommandPalette } from "@/components/console/CommandPalette";
import { Shortcuts } from "@/components/console/Shortcuts";
import { LoginScreen } from "@/components/console/LoginScreen";
import { useHotkeys } from "@/lib/use-hotkeys";
import {
  broadcastSyncComplete,
  NEW_MAIL_SCAN_LIMIT,
  useAutoSync,
} from "@/lib/use-auto-sync";
import { Toaster } from "@/components/ui/sonner";
import { ConsoleLayout } from "@/components/console/ConsoleLayout";
import { UI_KEY, loadUi } from "@/lib/layout";
import type { Arrangement, PaneLayout, PaneSizes } from "@/lib/layout";
import { applyTheme, resolveTheme, watchSystemTheme } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";
import { PanelRightOpen, Search, X } from "lucide-react";

type SortMode = "recent" | "confidence_asc" | "confidence_desc";

// Which chrome panels are visible (`prediction` is the collapsible bar inside
// the detail pane). Persisted (with the pane arrangement and sizes, see
// lib/layout.ts) so the operator's layout sticks.
// The shortcuts hint lives inside the sidebar, so it tracks `sidebar`.
type Panels = { sidebar: boolean; detail: boolean; prediction: boolean };

// One read at module load; the states below fan out from it.
const INITIAL_UI = loadUi();

// How long the "thread deleted · undo" window stays open before the delete is
// actually sent to the server.
const UNDO_MS = 5000;

// Guards the one-time OAuth code exchange. The authorization code is single-use,
// but React StrictMode invokes effects twice in dev — without this module-level
// latch the second run would re-POST the spent code and get `invalid_grant`.
let oauthExchangeStarted = false;

export default function Console() {
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  const [bucket, setBucket] = useState<BucketKey>("needs_reply");
  const [items, setItems] = useState<TriageItem[]>([]);
  const [allCounts, setAllCounts] = useState<Record<BucketKey, number>>({
    needs_reply: 0,
    action_required: 0,
    fyi: 0,
    promotional: 0,
    security_alert: 0,
    spam: 0,
    all: 0,
    unclassified: 0,
    done: 0,
  });
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [overview, setOverview] = useState<Overview | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [threadError, setThreadError] = useState<string | null>(null);

  const [ingesting, setIngesting] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [ingestOpen, setIngestOpen] = useState(false);
  const [backfillOpen, setBackfillOpen] = useState(false);

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>("recent");

  const [panels, setPanels] = useState<Panels>({
    sidebar: INITIAL_UI.sidebar,
    detail: INITIAL_UI.detail,
    prediction: INITIAL_UI.prediction,
  });
  const [arrangement, setArrangement] = useState<Arrangement>(
    INITIAL_UI.arrangement,
  );
  const [paneSizes, setPaneSizes] = useState<PaneSizes>(INITIAL_UI.paneSizes);
  const [layoutOpen, setLayoutOpen] = useState(false);
  const [theme, setTheme] = useState<ThemePref>(INITIAL_UI.theme);
  const [autoSync, setAutoSync] = useState<number>(INITIAL_UI.autoSync);
  // Resolved dark/light, tracked as state so theme-aware children (the
  // toaster) re-render when a "system" preference follows an OS flip.
  const [resolvedTheme, setResolvedTheme] = useState<"dark" | "light">(() =>
    resolveTheme(INITIAL_UI.theme),
  );

  useEffect(() => {
    applyTheme(theme);
    setResolvedTheme(resolveTheme(theme));
    return watchSystemTheme(theme, setResolvedTheme);
  }, [theme]);

  // Search: `query` drives the instant client-side filter of the loaded bucket;
  // running a search (Enter) flips `searchMode` on and shows whole-mailbox
  // results from the server instead.
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState(false);
  const [searchResults, setSearchResults] = useState<TriageItem[]>([]);
  const [searching, setSearching] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  // The thread-list scroll container, so the new-mail pill can jump to top.
  const listScrollRef = useRef<HTMLDivElement>(null);

  const gPressedAt = useRef<number>(0);
  // Timed `l` prefix (Gmail-style "label"): l then 1-6 reclassifies the
  // focused thread. Plain digits still switch buckets.
  const lPressedAt = useRef<number>(0);
  // thread_id -> timer for deletes still inside their undo window.
  const pendingDeletes = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(
        UI_KEY,
        JSON.stringify({ ...panels, arrangement, paneSizes, theme, autoSync }),
      );
    } catch {
      /* storage unavailable; layout just won't persist */
    }
  }, [panels, arrangement, paneSizes, theme, autoSync]);

  const togglePanel = useCallback((key: keyof Panels) => {
    setPanels((p) => ({ ...p, [key]: !p[key] }));
  }, []);

  const handlePaneSizes = useCallback((key: string, layout: PaneLayout) => {
    setPaneSizes((s) => ({ ...s, [key]: layout }));
  }, []);

  // ---- auth ----------------------------------------------------------------
  useEffect(() => {
    (async () => {
      // If we're landing on the Google OAuth callback, exchange the `code` for a
      // session token before the normal session check. The redirect URL is then
      // scrubbed so a refresh doesn't replay a single-use code.
      if (
        window.location.pathname.endsWith("/auth/google/callback") &&
        !oauthExchangeStarted
      ) {
        oauthExchangeStarted = true;
        const params = new URLSearchParams(window.location.search);
        const code = params.get("code");
        const oauthError = params.get("error");
        try {
          if (oauthError) {
            toast.error(`google sign-in cancelled (${oauthError})`);
          } else if (code) {
            const res = await googleAuthCallback(code, params.get("state"));
            setToken(res.access_token);
          }
        } catch (e) {
          toast.error((e as Error).message || "google sign-in failed");
        } finally {
          window.history.replaceState({}, "", "/");
        }
      }

      try {
        const me = await getMe();
        setUser(me);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setToken(null);
        }
        setUser(null);
      } finally {
        setAuthChecked(true);
      }
    })();
  }, []);

  const handleSessionExpired = useCallback(() => {
    setToken(null);
    setUser(null);
    toast.error("session expired — please sign in again");
  }, []);

  // ---- data fetching -------------------------------------------------------
  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await getOverview());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleSessionExpired();
    }
  }, [handleSessionExpired]);

  const refreshCounts = useCallback(async () => {
    // Server aggregates counts across the whole mailbox, so the sidebar totals
    // don't cap at a single triage page.
    try {
      setAllCounts(await getCounts());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleSessionExpired();
    }
  }, [handleSessionExpired]);

  const refreshList = useCallback(
    async (b: BucketKey, opts?: { quiet?: boolean }) => {
      const quiet = opts?.quiet ?? false;
      // Quiet refreshes (background sync) skip the loading flash and never
      // touch the selection — rows may shift, but the open thread stays open.
      if (!quiet) setListLoading(true);
      setListError(null);
      try {
        const res = await getTriage(b, 200);
        // Rows mid-undo-window are already gone from the UI but not yet from
        // the server; a refresh must not resurrect them.
        const rows = res.items.filter((i) => !pendingDeletes.current.has(i.thread_id));
        setItems(rows);
        if (!quiet) {
          // ensure a valid selection
          setSelectedId((prev) => {
            if (prev && rows.some((i) => i.thread_id === prev)) return prev;
            return rows[0]?.thread_id ?? null;
          });
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          handleSessionExpired();
          return;
        }
        setListError((e as Error).message ?? "failed to load");
      } finally {
        if (!quiet) setListLoading(false);
      }
    },
    [handleSessionExpired],
  );

  const refreshAll = useCallback(
    (opts?: { quiet?: boolean }) =>
      Promise.all([refreshList(bucket, opts), refreshOverview(), refreshCounts()]),
    [bucket, refreshList, refreshOverview, refreshCounts],
  );

  // Background sync: quiet periodic new-only ingest. After any sync that
  // changed the DB the whole console refreshes in place (no loading flash,
  // selection kept) and the "N new" pill re-derives from the persisted
  // acknowledged-mail watermark — so it survives reloads and syncs this tab
  // never saw finish. An empty mailbox pauses the loop entirely: there's no
  // baseline to sync against until the first manual ingest (which refreshes
  // the overview and thereby resumes it).
  const { pendingNew, clearNew, syncFailed } = useAutoSync({
    intervalSec: autoSync,
    enabled: !!user && (overview?.summary.threads ?? 0) > 0,
    busy: ingesting || backfilling,
    userId: user?.id ?? null,
    onSessionExpired: handleSessionExpired,
    onSynced: () => refreshAll({ quiet: true }).then(() => undefined),
  });

  // initial + bucket changes. Switching buckets also exits any active search.
  // The new-mail pill deliberately survives bucket switches: it clears only on
  // explicit acknowledgment (pill click, `r`, manual ingest).
  useEffect(() => {
    if (!user) return;
    setSearchMode(false);
    setSearchResults([]);
    setQuery("");
    refreshList(bucket);
  }, [user, bucket, refreshList]);

  useEffect(() => {
    if (!user) return;
    refreshOverview();
    refreshCounts();
  }, [user, refreshOverview, refreshCounts]);

  // thread detail
  useEffect(() => {
    if (!selectedId) {
      setThread(null);
      return;
    }
    let cancelled = false;
    setThreadLoading(true);
    setThreadError(null);
    getThread(selectedId)
      .then((d) => {
        if (!cancelled) setThread(d);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          handleSessionExpired();
          return;
        }
        setThreadError((e as Error).message ?? "failed to load thread");
      })
      .finally(() => {
        if (!cancelled) setThreadLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, handleSessionExpired]);

  // ---- derived: sorted items ----------------------------------------------
  const sortedItems = useMemo(() => {
    if (sortMode === "recent") return items;
    const arr = [...items];
    arr.sort((a, b) => {
      const ac = a.classification.confidence ?? -1;
      const bc = b.classification.confidence ?? -1;
      return sortMode === "confidence_asc" ? ac - bc : bc - ac;
    });
    return arr;
  }, [items, sortMode]);

  // What the list actually shows: whole-mailbox search results when a search is
  // running, otherwise the bucket list with the instant client-side filter
  // applied on top.
  const visibleItems = useMemo(() => {
    if (searchMode) return searchResults;
    const q = query.trim().toLowerCase();
    if (!q) return sortedItems;
    return sortedItems.filter(
      (it) =>
        (it.subject ?? "").toLowerCase().includes(q) ||
        (it.latest_message_snippet ?? "").toLowerCase().includes(q),
    );
  }, [searchMode, searchResults, query, sortedItems]);

  const selectedIndex = useMemo(
    () => visibleItems.findIndex((i) => i.thread_id === selectedId),
    [visibleItems, selectedId],
  );

  const focusedItem = selectedIndex >= 0 ? visibleItems[selectedIndex] : null;

  // ---- actions -------------------------------------------------------------
  const moveSelection = useCallback(
    (delta: number) => {
      if (visibleItems.length === 0) return;
      const cur = selectedIndex < 0 ? 0 : selectedIndex;
      const next = Math.max(0, Math.min(visibleItems.length - 1, cur + delta));
      const target = visibleItems[next];
      setSelectedId(target.thread_id);
      // scroll into view
      requestAnimationFrame(() => {
        const el = document.querySelector(
          `[data-thread-row="${target.thread_id}"]`,
        );
        if (el && "scrollIntoView" in el) {
          (el as HTMLElement).scrollIntoView({ block: "nearest" });
        }
      });
    },
    [visibleItems, selectedIndex],
  );

  // ---- search --------------------------------------------------------------
  const runSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    try {
      const res = await searchThreads(q, 100);
      const rows = res.items.filter((i) => !pendingDeletes.current.has(i.thread_id));
      setSearchResults(rows);
      setSearchMode(true);
      setSelectedId(rows[0]?.thread_id ?? null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        handleSessionExpired();
        return;
      }
      toast.error((e as Error).message ?? "search failed");
    } finally {
      setSearching(false);
    }
  }, [query, handleSessionExpired]);

  const clearSearch = useCallback(() => {
    setQuery("");
    setSearchMode(false);
    setSearchResults([]);
    searchInputRef.current?.blur();
  }, []);

  // ---- delete (optimistic, with a deferred server call for undo) -----------
  const doDelete = useCallback(
    (idArg?: string) => {
      const id = idArg ?? selectedId;
      if (!id || pendingDeletes.current.has(id)) return;

      // Snapshot position in each list so undo can put the row back exactly.
      const bucketIdx = items.findIndex((i) => i.thread_id === id);
      const removedFromBucket = bucketIdx >= 0 ? items[bucketIdx] : null;
      const resultIdx = searchResults.findIndex((i) => i.thread_id === id);
      const removedFromResults = resultIdx >= 0 ? searchResults[resultIdx] : null;

      // Advance selection to a neighbour in the visible list before removing.
      if (selectedId === id) {
        const vi = visibleItems.findIndex((i) => i.thread_id === id);
        const next = visibleItems[vi + 1] ?? visibleItems[vi - 1] ?? null;
        setSelectedId(next?.thread_id ?? null);
      }
      setItems((prev) => prev.filter((i) => i.thread_id !== id));
      setSearchResults((prev) => prev.filter((i) => i.thread_id !== id));

      const undo = () => {
        const timer = pendingDeletes.current.get(id);
        if (timer) clearTimeout(timer);
        pendingDeletes.current.delete(id);
        if (removedFromBucket) {
          setItems((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(bucketIdx, copy.length), 0, removedFromBucket);
            return copy;
          });
        }
        if (removedFromResults) {
          setSearchResults((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(resultIdx, copy.length), 0, removedFromResults);
            return copy;
          });
        }
        setSelectedId(id);
      };

      const timer = setTimeout(async () => {
        pendingDeletes.current.delete(id);
        try {
          await deleteThread(id);
          // Full quiet refresh: counts AND the list, so anything a background
          // refresh showed during the undo window reconciles right here.
          refreshAll({ quiet: true });
        } catch (e) {
          toast.error((e as Error).message ?? "delete failed");
          undo(); // put it back if the server rejected it
        }
      }, UNDO_MS);
      pendingDeletes.current.set(id, timer);

      toast("thread deleted", {
        description: "removing in a few seconds",
        action: { label: "undo", onClick: undo },
        duration: UNDO_MS,
      });
    },
    [selectedId, items, searchResults, visibleItems, refreshAll],
  );

  // Deletes still inside their undo window must survive the page going away —
  // otherwise a reload quietly resurrects "deleted" threads.
  useEffect(() => {
    const flush = () => {
      for (const [id, timer] of pendingDeletes.current) {
        clearTimeout(timer);
        flushDeleteThread(id);
      }
      pendingDeletes.current.clear();
    };
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  // ---- done (non-destructive exit from triage; inverse action in `done`) ----
  const doDone = useCallback(
    (idArg?: string) => {
      const id = idArg ?? selectedId;
      if (!id) return;
      // In the done bucket the same action restores the thread instead.
      const marking = bucket !== "done";

      // Snapshot the row so undo (or a rejected call) can put it back exactly.
      const bucketIdx = items.findIndex((i) => i.thread_id === id);
      const removed = bucketIdx >= 0 ? items[bucketIdx] : null;

      // Flow mode: hand focus to a neighbour before the row disappears. In
      // search mode the row stays (done threads remain searchable), so the
      // selection stays put too.
      if (selectedId === id && !searchMode) {
        const vi = visibleItems.findIndex((i) => i.thread_id === id);
        const next = visibleItems[vi + 1] ?? visibleItems[vi - 1] ?? null;
        setSelectedId(next?.thread_id ?? null);
      }
      setItems((prev) => prev.filter((i) => i.thread_id !== id));

      const restore = () => {
        if (removed) {
          setItems((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(bucketIdx, copy.length), 0, removed);
            return copy;
          });
        }
        setSelectedId(id);
      };

      void (async () => {
        try {
          await setThreadDone(id, marking);
          refreshCounts();
          toast(marking ? "thread done" : "thread restored", {
            action: {
              label: "undo",
              onClick: () => {
                setThreadDone(id, !marking)
                  .then(() => {
                    restore();
                    refreshCounts();
                  })
                  .catch((e) =>
                    toast.error((e as Error).message ?? "undo failed"),
                  );
              },
            },
          });
        } catch (e) {
          restore();
          toast.error(
            (e as Error).message ?? (marking ? "done failed" : "restore failed"),
          );
        }
      })();
    },
    [selectedId, bucket, items, searchMode, visibleItems, refreshCounts],
  );

  // Jump to the thread in the Gmail web UI (default signed-in account).
  const openInGmail = useCallback(() => {
    const t = thread?.thread;
    if (!t || t.id !== selectedId) return;
    if (t.provider !== "gmail" || !t.provider_thread_id) return;
    window.open(gmailThreadUrl(t.provider_thread_id), "_blank", "noopener");
  }, [thread, selectedId]);

  // Shared tail for every queued worker job (ingest / backfill / classify):
  // the API answers 202 as soon as the task is QUEUED, so wait for the worker
  // to actually finish before refreshing — otherwise the UI refetches a
  // mailbox the worker hasn't written to yet.
  const trackTask = useCallback(
    async (
      taskId: string,
      label: string,
      summarize: (res: TaskResult) => string,
    ) => {
      const t = toast.loading(`${label} running…`);
      const final = await waitForTask(taskId, {
        timeoutMs: WORKER_TASK_TIMEOUT_MS,
      });
      const res = final.result;
      if (res?.status === "error") {
        toast.error(res.detail ?? `${label} failed`, { id: t });
        return;
      }
      if (final.error) {
        toast.error(final.error, { id: t });
        return;
      }
      if (!final.ready) {
        toast.message(`${label} still running — showing what's landed so far`, { id: t });
      } else {
        toast.success(summarize(res ?? {}), { id: t });
      }
      await refreshAll();
    },
    [refreshAll],
  );

  const doIngest = useCallback(
    async (opts: IngestOptions) => {
      setIngesting(true);
      try {
        const intendedMode = opts.refreshExisting ? "refresh" : "manual";
        let r = await ingestGmail(opts.maxResults, opts.classify, opts.refreshExisting);
        let final = r;
        let retried = false;
        for (;;) {
          if (r.deduplicated) {
            toast("another sync is already running — waiting for it");
          }
          final = r.ready ? r : await waitForSyncRun(r.run_id);
          if (!final.ready) {
            toast.message("ingest still running in the background — showing what's landed so far");
            await refreshAll({ quiet: true });
            return;
          }
          if (!r.deduplicated || r.mode === intendedMode) break;
          if (retried) {
            toast("another sync took precedence again — please try again");
            return;
          }
          retried = true;
          r = await ingestGmail(opts.maxResults, opts.classify, opts.refreshExisting);
        }
        if (final.status === "failed" || final.result?.status === "error") {
          throw new Error(final.result?.detail ?? final.error ?? "ingest failed");
        }
        const res = final.result ?? {};
        toast.success(
          `ingest complete · ${res.threads_upserted ?? r.new_threads ?? 0} threads · ` +
            `${res.messages_upserted ?? 0} msgs`,
        );
        await refreshAll();
        if (user) broadcastSyncComplete(user.id);
        // A deliberate pull acknowledges everything it just surfaced.
        clearNew();
      } catch (e) {
        toast.error((e as Error).message ?? "ingest failed");
      } finally {
        setIngesting(false);
      }
    },
    [refreshAll, clearNew, user],
  );

  const doBackfill = useCallback(
    async (opts: BackfillOptions) => {
      setBackfilling(true);
      try {
        const r = await classifyBackfill(opts);
        if (r.status === "queued") {
          // Big batch went to the worker; wait it out like ingest does.
          await trackTask(
            r.task_id,
            "backfill",
            (res) =>
              `backfill complete · ${res.created ?? 0} classified · ` +
              `${res.scanned ?? 0} scanned`,
          );
          return;
        }
        toast.success(`classified ${r.created} · scanned ${r.scanned}`);
        await refreshAll();
      } catch (e) {
        toast.error((e as Error).message ?? "backfill failed");
      } finally {
        setBackfilling(false);
      }
    },
    [trackTask, refreshAll],
  );

  const doQueue = useCallback(async () => {
    try {
      const r = await classifyQueue(200, false);
      await trackTask(
        r.task_id,
        "classify",
        (res) =>
          `classify complete · ${res.created ?? 0} new labels · ` +
          `${res.processed ?? 0} processed`,
      );
    } catch (e) {
      toast.error((e as Error).message ?? "queue failed");
    }
  }, [trackTask]);

  const doReclassify = useCallback(
    async (label: Label) => {
      const id = selectedId;
      if (!id) return;
      // Flow mode: a relabeled thread leaves a bucket it no longer matches,
      // and the selection moves on either way. "all", "done", and search
      // results keep the row (with its new label) in place.
      const leavesBucket =
        !searchMode &&
        (bucket === "unclassified" ||
          (bucket !== "all" && bucket !== "done" && bucket !== label));
      const bucketIdx = items.findIndex((i) => i.thread_id === id);
      const removed = bucketIdx >= 0 ? items[bucketIdx] : null;
      const vi = visibleItems.findIndex((i) => i.thread_id === id);
      const next =
        visibleItems[vi + 1] ?? (leavesBucket ? visibleItems[vi - 1] : null);
      if (vi >= 0 && (next || leavesBucket)) {
        setSelectedId(next?.thread_id ?? null);
      }

      // Snapshot the row (captured from the updaters' live state, so we don't
      // depend on `items` here) to roll back if the server rejects the change.
      // Search results are a separate list rendered while searchMode is on, so
      // the optimistic write (and any rollback) has to hit both.
      let prevItem: TriageItem | undefined;
      const applyOverride = (it: TriageItem): TriageItem => {
        if (it.thread_id !== id) return it;
        prevItem = it;
        return {
          ...it,
          classification: {
            label,
            confidence: 1,
            model_version: "user-override",
          },
        };
      };
      setItems((prev) => {
        const mapped = prev.map(applyOverride);
        return leavesBucket ? mapped.filter((it) => it.thread_id !== id) : mapped;
      });
      setSearchResults((prev) => prev.map(applyOverride));
      try {
        await reclassify(id, label);
        toast.success(`label → ${label}`);
        refreshCounts();
      } catch (e) {
        // Restore the pre-optimistic label (and the row itself, if flow mode
        // dropped it from the bucket) so nothing keeps a change the server
        // never accepted.
        if (leavesBucket && removed) {
          setItems((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(bucketIdx, copy.length), 0, removed);
            return copy;
          });
        } else if (prevItem) {
          const restored = prevItem;
          setItems((prev) =>
            prev.map((it) => (it.thread_id === id ? restored : it)),
          );
        }
        if (prevItem) {
          const restored = prevItem;
          setSearchResults((prev) =>
            prev.map((it) => (it.thread_id === id ? restored : it)),
          );
        }
        setSelectedId(id);
        toast.error((e as Error).message ?? "reclassify failed");
      }
    },
    [selectedId, bucket, items, searchMode, visibleItems, refreshCounts],
  );

  // ---- hotkeys -------------------------------------------------------------
  useHotkeys(
    (e) => {
      // overlays open: only handle escape
      if (paletteOpen || shortcutsOpen) {
        if (e.key === "Escape") {
          setPaletteOpen(false);
          setShortcutsOpen(false);
        }
        return;
      }

      // Escape clears an active search (works even while the search box has
      // focus, since useHotkeys lets Escape through).
      if (e.key === "Escape") {
        if (searchMode || query) {
          e.preventDefault();
          clearSearch();
        }
        return;
      }

      // palette
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (e.key === "?") {
        e.preventDefault();
        setShortcutsOpen(true);
        return;
      }
      // panels + search + delete
      if (e.key === "/") {
        e.preventDefault();
        searchInputRef.current?.focus();
        return;
      }
      if (e.key === "[") {
        e.preventDefault();
        togglePanel("sidebar");
        return;
      }
      if (e.key === "]") {
        e.preventDefault();
        togglePanel("detail");
        return;
      }
      if (e.key === "#") {
        e.preventDefault();
        doDelete();
        return;
      }
      if (e.key === "e") {
        e.preventDefault();
        doDone();
        return;
      }
      if (e.key === "o") {
        e.preventDefault();
        openInGmail();
        return;
      }
      if (e.key === "l") {
        lPressedAt.current = Date.now();
        return;
      }
      // l then 1-6: relabel the focused thread (checked before bucket
      // switching so the digit doesn't change buckets instead).
      if (Date.now() - lPressedAt.current < 800 && /^[1-6]$/.test(e.key)) {
        e.preventDefault();
        lPressedAt.current = 0;
        doReclassify(ALL_LABELS[Number(e.key) - 1]);
        return;
      }
      // bucket switching 1-9
      for (const [b, key] of Object.entries(BUCKET_KEYS)) {
        if (e.key === key) {
          e.preventDefault();
          setBucket(b as BucketKey);
          return;
        }
      }
      if (e.key === "j") {
        e.preventDefault();
        moveSelection(1);
      } else if (e.key === "k") {
        e.preventDefault();
        moveSelection(-1);
      } else if (e.key === "G") {
        e.preventDefault();
        if (visibleItems.length)
          setSelectedId(visibleItems[visibleItems.length - 1].thread_id);
      } else if (e.key === "g") {
        const now = Date.now();
        if (now - gPressedAt.current < 400) {
          if (visibleItems.length) setSelectedId(visibleItems[0].thread_id);
          gPressedAt.current = 0;
        } else {
          gPressedAt.current = now;
        }
      } else if (e.key === "Enter") {
        // already selected; this is a no-op besides ensuring scroll
        if (focusedItem)
          document
            .querySelector(`[data-thread-row="${focusedItem.thread_id}"]`)
            ?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "c") {
        setSortMode((m) =>
          m === "confidence_asc"
            ? "confidence_desc"
            : m === "confidence_desc"
              ? "recent"
              : "confidence_asc",
        );
      } else if (e.key === "r") {
        clearNew();
        refreshList(bucket);
        refreshOverview();
        refreshCounts();
      } else if (e.key === "i") {
        doIngest({ maxResults: 100, classify: true, refreshExisting: false });
      } else if (e.key === "b") {
        doBackfill({ limit: 200, bucket, backend: "local", force: false });
      } else if (e.key === "q") {
        doQueue();
      }
    },
    [
      paletteOpen,
      shortcutsOpen,
      searchMode,
      query,
      visibleItems,
      focusedItem,
      bucket,
      moveSelection,
      clearSearch,
      togglePanel,
      doDelete,
      doDone,
      doReclassify,
      openInGmail,
      refreshList,
      refreshOverview,
      refreshCounts,
      doIngest,
      doBackfill,
      doQueue,
      clearNew,
    ],
  );

  // ---- render --------------------------------------------------------------
  if (!authChecked) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground font-mono text-sm">
        checking session…
      </div>
    );
  }
  if (!user) {
    return (
      <>
        <LoginScreen
          onAuthed={(u) => {
            setUser(u);
          }}
        />
        <Toaster theme={resolvedTheme} />
      </>
    );
  }

  const sortBadge =
    sortMode === "recent"
      ? "recent"
      : sortMode === "confidence_asc"
        ? "conf ↑"
        : "conf ↓";

  return (
    // overflow-clip: no descendant (however hostile an email's CSS) may ever
    // grow the page a scrollbar — panes own all scrolling.
    <div className="h-screen flex flex-col overflow-clip bg-background text-foreground">
      <TopBar
        user={user}
        overview={overview}
        ingesting={ingesting}
        backfilling={backfilling}
        currentBucket={bucket}
        onIngest={doIngest}
        onBackfill={doBackfill}
        ingestOpen={ingestOpen}
        onIngestOpenChange={setIngestOpen}
        backfillOpen={backfillOpen}
        onBackfillOpenChange={setBackfillOpen}
        layoutOpen={layoutOpen}
        onLayoutOpenChange={setLayoutOpen}
        arrangement={arrangement}
        onArrangement={setArrangement}
        theme={theme}
        onTheme={setTheme}
        autoSync={autoSync}
        onAutoSync={setAutoSync}
        onLogout={() => {
          setToken(null);
          setUser(null);
        }}
      />

      <ConsoleLayout
        arrangement={arrangement}
        onArrangementChange={setArrangement}
        sidebarVisible={panels.sidebar}
        detailVisible={panels.detail}
        onExpandSidebar={() => togglePanel("sidebar")}
        paneSizes={paneSizes}
        onPaneSizesChange={handlePaneSizes}
        sidebar={
          <BucketSidebar
            active={bucket}
            counts={allCounts}
            onSelect={(b) => setBucket(b)}
            onCollapse={() => togglePanel("sidebar")}
            side={arrangement.sidebar}
          />
        }
        list={
          <section className="flex-1 min-w-0 min-h-0 flex flex-col">
          <div className="h-10 shrink-0 border-b border-border bg-[var(--color-panel)] panel-lift flex items-center px-3 gap-2.5 font-mono text-[11.5px]">
            <span className="text-primary font-semibold tracking-tight shrink-0">
              {searchMode ? "search" : bucket.replace("_", " ")}
            </span>
            <span className="text-muted-foreground tabular-nums shrink-0">
              {visibleItems.length}
              {searchMode ? " match" : " thread"}
              {visibleItems.length === 1 ? "" : "s"}
              {searchMode ? " · all buckets" : ""}
            </span>

            {pendingNew > 0 && (
              <button
                data-testid="new-mail-pill"
                onClick={() => {
                  clearNew();
                  refreshAll();
                  listScrollRef.current?.scrollTo({ top: 0 });
                }}
                title="new mail — click to jump to the top"
                className="shrink-0 h-6 px-2 rounded-full border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20 tabular-nums cursor-pointer transition-colors"
              >
                {pendingNew >= NEW_MAIL_SCAN_LIMIT ? `${NEW_MAIL_SCAN_LIMIT}+` : pendingNew} new
              </button>
            )}
            {syncFailed && (
              <span
                data-testid="sync-failed-dot"
                title="auto-sync failing — retrying in the background"
                className="shrink-0 h-1.5 w-1.5 rounded-full bg-destructive/70"
              />
            )}

            <div className="flex-1 flex items-center min-w-0 max-w-[380px] ml-1">
              <div className="flex items-center gap-1.5 w-full rounded border border-border bg-background px-2 h-6 focus-within:border-primary transition-colors">
                <Search className="h-3 w-3 text-muted-foreground shrink-0" />
                <input
                  ref={searchInputRef}
                  value={query}
                  onChange={(e) => {
                    setQuery(e.target.value);
                    if (searchMode) {
                      setSearchMode(false);
                      setSearchResults([]);
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      runSearch();
                    }
                  }}
                  placeholder="filter…  ↵ to search all buckets"
                  className="flex-1 min-w-0 bg-transparent outline-none text-[12px] placeholder:text-muted-foreground/60"
                />
                {(query || searchMode) && (
                  <button
                    onClick={clearSearch}
                    aria-label="Clear search"
                    className="shrink-0 text-muted-foreground hover:text-foreground cursor-pointer"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            </div>

            <button
              onClick={() =>
                setSortMode((m) =>
                  m === "confidence_asc"
                    ? "confidence_desc"
                    : m === "confidence_desc"
                      ? "recent"
                      : "confidence_asc",
                )
              }
              title="press c"
              aria-label={`Sort order: ${sortBadge}. Press c to cycle.`}
              className="shrink-0 px-2 py-0.5 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              sort: {sortBadge}
            </button>

            {!panels.detail && (
              <button
                onClick={() => togglePanel("detail")}
                aria-label="Show thread detail"
                title="Show detail ( ] )"
                className="shrink-0 h-6 px-1.5 rounded border border-border text-muted-foreground hover:text-foreground hover:bg-accent cursor-pointer transition-colors flex items-center"
              >
                <PanelRightOpen className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
            <div ref={listScrollRef} className="flex-1 overflow-y-auto scrollbar-thin">
              <ThreadList
                items={visibleItems}
                selectedId={selectedId}
                onSelect={(id) => setSelectedId(id)}
                loading={listLoading || searching}
                error={listError}
              />
            </div>
          </section>
        }
        detail={
          <ThreadDetailPane
            data={thread}
            classification={focusedItem?.classification ?? null}
            loading={threadLoading}
            error={threadError}
            onReclassify={doReclassify}
            onCollapse={() => togglePanel("detail")}
            onDone={focusedItem ? () => doDone(focusedItem.thread_id) : undefined}
            onDelete={
              focusedItem ? () => doDelete(focusedItem.thread_id) : undefined
            }
            side={arrangement.reading}
            predictionOpen={panels.prediction}
            onTogglePrediction={() => togglePanel("prediction")}
          />
        }
      />

      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        onBucket={(b) => setBucket(b)}
        onIngest={() => setIngestOpen(true)}
        onBackfill={() => setBackfillOpen(true)}
        onQueue={doQueue}
        onReclassify={doReclassify}
        hasFocusedThread={!!focusedItem}
        onToggleSidebar={() => togglePanel("sidebar")}
        onToggleDetail={() => togglePanel("detail")}
        onTogglePrediction={() => togglePanel("prediction")}
        onTheme={setTheme}
        onAutoSync={setAutoSync}
        onArrangement={(patch) => setArrangement((a) => ({ ...a, ...patch }))}
        onFocusSearch={() =>
          setTimeout(() => searchInputRef.current?.focus(), 60)
        }
        onDone={() => doDone()}
        inDoneBucket={bucket === "done"}
        onOpenGmail={openInGmail}
        onDelete={() => doDelete()}
      />
      <Shortcuts open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
      <Toaster theme={resolvedTheme} />
    </div>
  );
}
