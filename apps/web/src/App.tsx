import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  classifyBackfill,
  classifyQueue,
  deleteThread,
  getCounts,
  getMe,
  googleAuthCallback,
  getOverview,
  getThread,
  getTriage,
  ingestGmail,
  reclassify,
  searchThreads,
  setToken,
  waitForTask,
} from "@/lib/api";
import type { TaskResult } from "@/lib/api";
import { BUCKET_KEYS } from "@/lib/labels";
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
import { toast } from "sonner";

import { BucketSidebar } from "@/components/console/BucketSidebar";
import { ThreadList } from "@/components/console/ThreadList";
import { ThreadDetailPane } from "@/components/console/ThreadDetailPane";
import { TopBar } from "@/components/console/TopBar";
import { CommandPalette } from "@/components/console/CommandPalette";
import { Shortcuts } from "@/components/console/Shortcuts";
import { LoginScreen } from "@/components/console/LoginScreen";
import { useHotkeys } from "@/lib/use-hotkeys";
import { Toaster } from "@/components/ui/sonner";
import { PanelLeftOpen, PanelRightOpen, Search, X } from "lucide-react";

type SortMode = "recent" | "confidence_asc" | "confidence_desc";

// Which chrome panels are visible. Persisted so the operator's layout sticks.
// The shortcuts hint lives inside the sidebar, so it tracks `sidebar`.
type Panels = { sidebar: boolean; detail: boolean };
const UI_KEY = "ai_mailbox_ui";
const DEFAULT_PANELS: Panels = { sidebar: true, detail: true };

function loadPanels(): Panels {
  if (typeof window === "undefined") return DEFAULT_PANELS;
  try {
    const raw = window.localStorage.getItem(UI_KEY);
    if (raw) return { ...DEFAULT_PANELS, ...JSON.parse(raw) };
  } catch {
    /* fall through to defaults */
  }
  return DEFAULT_PANELS;
}

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

  const [panels, setPanels] = useState<Panels>(loadPanels);

  // Search: `query` drives the instant client-side filter of the loaded bucket;
  // running a search (Enter) flips `searchMode` on and shows whole-mailbox
  // results from the server instead.
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState(false);
  const [searchResults, setSearchResults] = useState<TriageItem[]>([]);
  const [searching, setSearching] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);

  const gPressedAt = useRef<number>(0);
  // thread_id -> timer for deletes still inside their undo window.
  const pendingDeletes = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(UI_KEY, JSON.stringify(panels));
    } catch {
      /* storage unavailable; layout just won't persist */
    }
  }, [panels]);

  const togglePanel = useCallback((key: keyof Panels) => {
    setPanels((p) => ({ ...p, [key]: !p[key] }));
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
    async (b: BucketKey) => {
      setListLoading(true);
      setListError(null);
      try {
        const res = await getTriage(b, 200);
        setItems(res.items);
        // ensure a valid selection
        setSelectedId((prev) => {
          if (prev && res.items.some((i) => i.thread_id === prev)) return prev;
          return res.items[0]?.thread_id ?? null;
        });
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          handleSessionExpired();
          return;
        }
        setListError((e as Error).message ?? "failed to load");
      } finally {
        setListLoading(false);
      }
    },
    [handleSessionExpired],
  );

  // initial + bucket changes. Switching buckets also exits any active search.
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
      setSearchResults(res.items);
      setSearchMode(true);
      setSelectedId(res.items[0]?.thread_id ?? null);
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
          refreshCounts();
          refreshOverview();
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
    [selectedId, items, searchResults, visibleItems, refreshCounts, refreshOverview],
  );

  const refreshAll = useCallback(
    () => Promise.all([refreshList(bucket), refreshOverview(), refreshCounts()]),
    [bucket, refreshList, refreshOverview, refreshCounts],
  );

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
      const final = await waitForTask(taskId);
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
        const r = await ingestGmail(opts.maxResults, opts.classify);
        // Mock / synchronous path: no task to wait on, data is already there.
        if (!r.task_id) {
          toast.success(`ingest complete · ${opts.maxResults} threads`);
          await refreshAll();
          return;
        }
        await trackTask(
          r.task_id,
          "ingest",
          (res) =>
            `ingest complete · ${res.threads_upserted ?? 0} threads · ` +
            `${res.messages_upserted ?? 0} msgs`,
        );
      } catch (e) {
        toast.error((e as Error).message ?? "ingest failed");
      } finally {
        setIngesting(false);
      }
    },
    [trackTask, refreshAll],
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
      setItems((prev) => prev.map(applyOverride));
      setSearchResults((prev) => prev.map(applyOverride));
      try {
        await reclassify(id, label);
        toast.success(`label → ${label}`);
        refreshCounts();
      } catch (e) {
        // Restore the pre-optimistic label so the row doesn't keep a change the
        // server never accepted.
        if (prevItem) {
          const restored = prevItem;
          setItems((prev) =>
            prev.map((it) => (it.thread_id === id ? restored : it)),
          );
          setSearchResults((prev) =>
            prev.map((it) => (it.thread_id === id ? restored : it)),
          );
        }
        toast.error((e as Error).message ?? "reclassify failed");
      }
    },
    [selectedId, refreshCounts],
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
      // bucket switching 1-8
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
        refreshList(bucket);
        refreshOverview();
        refreshCounts();
      } else if (e.key === "i") {
        doIngest({ maxResults: 100, classify: true });
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
      refreshList,
      refreshOverview,
      refreshCounts,
      doIngest,
      doBackfill,
      doQueue,
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
        <Toaster />
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
    <div className="h-screen flex flex-col bg-background text-foreground">
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
        onLogout={() => {
          setToken(null);
          setUser(null);
        }}
      />

      <div className="flex-1 min-h-0 flex">
        {panels.sidebar ? (
          <BucketSidebar
            active={bucket}
            counts={allCounts}
            onSelect={(b) => setBucket(b)}
            onCollapse={() => togglePanel("sidebar")}
          />
        ) : (
          <button
            onClick={() => togglePanel("sidebar")}
            aria-label="Show buckets"
            title="Show buckets ( [ )"
            className="w-8 shrink-0 border-r border-border bg-[var(--color-panel)] flex items-start justify-center pt-2.5 text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        )}

        <section className="flex-1 min-w-0 flex flex-col border-r border-border">
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
          <div className="flex-1 overflow-y-auto scrollbar-thin">
            <ThreadList
              items={visibleItems}
              selectedId={selectedId}
              onSelect={(id) => setSelectedId(id)}
              loading={listLoading || searching}
              error={listError}
            />
          </div>
        </section>

        {panels.detail && (
          <section className="w-[42%] min-w-[380px] max-w-[640px] flex flex-col bg-[var(--color-panel)]/30">
            <ThreadDetailPane
              data={thread}
              classification={focusedItem?.classification ?? null}
              loading={threadLoading}
              error={threadError}
              onReclassify={doReclassify}
              onCollapse={() => togglePanel("detail")}
              onDelete={
                focusedItem
                  ? () => doDelete(focusedItem.thread_id)
                  : undefined
              }
            />
          </section>
        )}
      </div>

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
        onFocusSearch={() =>
          setTimeout(() => searchInputRef.current?.focus(), 60)
        }
        onDelete={() => doDelete()}
      />
      <Shortcuts open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
      <Toaster />
    </div>
  );
}
