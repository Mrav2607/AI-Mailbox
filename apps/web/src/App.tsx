import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  classifyBackfill,
  classifyQueue,
  getMe,
  googleAuthCallback,
  getOverview,
  getThread,
  getTriage,
  ingestGmail,
  reclassify,
  setToken,
  USE_MOCK,
} from "@/lib/api";
import { mockApplyLabel } from "@/lib/mock";
import { BUCKET_KEYS } from "@/lib/labels";
import type {
  BucketKey,
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

type SortMode = "recent" | "confidence_asc" | "confidence_desc";

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

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>("recent");

  const gPressedAt = useRef<number>(0);

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
    // Fetch counts for each bucket using bucket=all and grouping client-side,
    // which keeps wire calls down to a single request.
    try {
      const all = await getTriage("all", 200);
      const counts: Record<BucketKey, number> = {
        needs_reply: 0,
        action_required: 0,
        fyi: 0,
        promotional: 0,
        security_alert: 0,
        spam: 0,
        all: all.items.length,
        unclassified: 0,
      };
      for (const it of all.items) {
        const l = it.classification.label;
        if (l) counts[l] += 1;
        else counts.unclassified += 1;
      }
      setAllCounts(counts);
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

  // initial + bucket changes
  useEffect(() => {
    if (!user) return;
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

  const selectedIndex = useMemo(
    () => sortedItems.findIndex((i) => i.thread_id === selectedId),
    [sortedItems, selectedId],
  );

  const focusedItem = selectedIndex >= 0 ? sortedItems[selectedIndex] : null;

  // ---- actions -------------------------------------------------------------
  const moveSelection = useCallback(
    (delta: number) => {
      if (sortedItems.length === 0) return;
      const cur = selectedIndex < 0 ? 0 : selectedIndex;
      const next = Math.max(0, Math.min(sortedItems.length - 1, cur + delta));
      const target = sortedItems[next];
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
    [sortedItems, selectedIndex],
  );

  const doIngest = useCallback(async () => {
    setIngesting(true);
    try {
      await ingestGmail(100);
      toast.success("ingest complete");
      await Promise.all([refreshList(bucket), refreshOverview(), refreshCounts()]);
    } catch (e) {
      toast.error((e as Error).message ?? "ingest failed");
    } finally {
      setIngesting(false);
    }
  }, [bucket, refreshList, refreshOverview, refreshCounts]);

  const doBackfill = useCallback(
    async (force = false) => {
      setBackfilling(true);
      try {
        await classifyBackfill(200, force);
        toast.success(force ? "re-classified all" : "backfill complete");
        await Promise.all([
          refreshList(bucket),
          refreshOverview(),
          refreshCounts(),
        ]);
      } catch (e) {
        toast.error((e as Error).message ?? "backfill failed");
      } finally {
        setBackfilling(false);
      }
    },
    [bucket, refreshList, refreshOverview, refreshCounts],
  );

  const doQueue = useCallback(async () => {
    try {
      const r = await classifyQueue(200, false);
      toast.success(`queued · ${r.task_id}`);
    } catch (e) {
      toast.error((e as Error).message ?? "queue failed");
    }
  }, []);

  const doReclassify = useCallback(
    async (label: Label) => {
      const id = selectedId;
      if (!id) return;
      // optimistic update
      setItems((prev) =>
        prev.map((it) =>
          it.thread_id === id
            ? {
                ...it,
                classification: {
                  label,
                  confidence: 1,
                  model_version: "operator-override",
                },
              }
            : it,
        ),
      );
      if (USE_MOCK) mockApplyLabel(id, label);
      try {
        await reclassify(id, label);
        toast.success(`label → ${label}`);
        refreshCounts();
      } catch (e) {
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
        if (sortedItems.length)
          setSelectedId(sortedItems[sortedItems.length - 1].thread_id);
      } else if (e.key === "g") {
        const now = Date.now();
        if (now - gPressedAt.current < 400) {
          if (sortedItems.length) setSelectedId(sortedItems[0].thread_id);
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
        doIngest();
      } else if (e.key === "b") {
        doBackfill();
      } else if (e.key === "q") {
        doQueue();
      }
    },
    [
      paletteOpen,
      shortcutsOpen,
      sortedItems,
      focusedItem,
      bucket,
      moveSelection,
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
        onIngest={doIngest}
        onBackfill={() => doBackfill(false)}
        onLogout={() => {
          setToken(null);
          setUser(null);
        }}
      />

      <div className="flex-1 min-h-0 flex">
        <BucketSidebar
          active={bucket}
          counts={allCounts}
          onSelect={(b) => setBucket(b)}
        />

        <section className="flex-1 min-w-0 flex flex-col border-r border-border">
          <div className="h-9 shrink-0 border-b border-border bg-[var(--color-panel)] flex items-center px-3 gap-3 font-mono text-[11.5px]">
            <span className="text-muted-foreground uppercase tracking-wider">
              {bucket.replace("_", " ")}
            </span>
            <span className="text-foreground/80 tabular-nums">
              {sortedItems.length} thread{sortedItems.length === 1 ? "" : "s"}
            </span>
            <div className="flex-1" />
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
              className="px-2 py-0.5 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground"
            >
              sort: {sortBadge}
            </button>
          </div>
          <div className="flex-1 overflow-y-auto scrollbar-thin">
            <ThreadList
              items={sortedItems}
              selectedId={selectedId}
              onSelect={(id) => setSelectedId(id)}
              loading={listLoading}
              error={listError}
            />
          </div>
        </section>

        <section className="w-[42%] min-w-[380px] max-w-[640px] flex flex-col bg-[var(--color-panel)]/30">
          <ThreadDetailPane
            data={thread}
            classification={focusedItem?.classification ?? null}
            loading={threadLoading}
            error={threadError}
            onReclassify={doReclassify}
          />
        </section>
      </div>

      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        onBucket={(b) => setBucket(b)}
        onIngest={doIngest}
        onBackfill={() => doBackfill(false)}
        onQueue={doQueue}
        onReclassify={doReclassify}
        hasFocusedThread={!!focusedItem}
      />
      <Shortcuts open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
      <Toaster />
    </div>
  );
}
