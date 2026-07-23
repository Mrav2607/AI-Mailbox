import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  allRunsDeduplicated,
  ApiError,
  classifyBackfill,
  classifyQueue,
  deleteConnection,
  deleteThread,
  flushDeleteThread,
  getCounts,
  getMe,
  getSyncHealth,
  googleAuthCallback,
  googleConnectCallback,
  googleConnectStart,
  getOverview,
  getThread,
  getTriage,
  ingestMail,
  listAuthProviders,
  listConnections,
  microsoftAuthCallback,
  microsoftConnectCallback,
  microsoftConnectStart,
  reclassify,
  searchThreads,
  setThreadDone,
  revokeAllTokens,
  setToken,
  sumIngestResults,
  waitForTask,
  waitForSyncRuns,
  WORKER_TASK_TIMEOUT_MS,
} from "@/lib/api";
import type { SyncHealth, SyncRunStatus, TaskResult } from "@/lib/api";
import { BUCKET_KEYS } from "@/lib/labels";
import { gmailThreadUrl } from "@/lib/utils";
import type {
  BackfillOptions,
  BucketKey,
  Connection,
  IngestOptions,
  Label,
  Overview,
  ThreadDetail,
  TriageItem,
  TriageSort,
  User,
} from "@/lib/types";
import { ALL_LABELS } from "@/lib/types";
import { toast } from "sonner";
import { createLiveSearch, type LiveSearchController } from "@/lib/live-search";
import { emailLocalPart } from "@/lib/sender";

import { BucketSidebar } from "@/components/console/BucketSidebar";
import { ThreadList } from "@/components/console/ThreadList";
import { ThreadDetailPane } from "@/components/console/ThreadDetailPane";
import { TopBar } from "@/components/console/TopBar";
import { CommandPalette } from "@/components/console/CommandPalette";
import { Shortcuts } from "@/components/console/Shortcuts";
import { LoginScreen } from "@/components/console/LoginScreen";
import { VerifyEmailScreen } from "@/components/console/VerifyEmailScreen";
import { ResetPasswordScreen } from "@/components/console/ResetPasswordScreen";
import { GoogleMark } from "@/components/console/GoogleMark";
import { MicrosoftMark } from "@/components/console/MicrosoftMark";
import {
  shouldSuppressConsoleHotkeys,
  useHotkeys,
} from "@/lib/use-hotkeys";
import {
  broadcastSyncComplete,
  NEW_MAIL_SCAN_LIMIT,
  useAutoSync,
} from "@/lib/use-auto-sync";
import { Toaster } from "@/components/ui/sonner";
import { ConsoleLayout } from "@/components/console/ConsoleLayout";
import { NarrowShell } from "@/components/console/NarrowShell";
import { TOUR_VERSION, UI_KEY, loadUi } from "@/lib/layout";
import type { Arrangement, Density, PaneLayout, PaneSizes } from "@/lib/layout";
import { isUnseen, loadSeen, markSeen } from "@/lib/seen";
import {
  useOnboardingTour,
  type TourDeps,
} from "@/lib/use-onboarding-tour";
import { useIsNarrow } from "@/lib/use-viewport";
import { applyTheme, resolveTheme, watchSystemTheme } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";
import { extractStateFromAuthUrl, saveOauthBinding, takeOauthBinding } from "@/lib/oauthBinding";
import { Mail, PanelRightOpen, Search, X } from "lucide-react";

type SortMode = "recent" | "confidence_asc" | "confidence_desc" | "account";

const nextSortMode = (m: SortMode): SortMode =>
  m === "recent"
    ? "confidence_asc"
    : m === "confidence_asc"
      ? "confidence_desc"
      : m === "confidence_desc"
        ? "account"
        : "recent";

// One triage page. Infinite scroll fetches this many rows at a time; the
// server telling us back fewer than this is how we know we've hit the end.
const PAGE_SIZE = 200;

// How long to wait after a keystroke before live search re-queries the
// server, and how short a query can be before it's not worth searching yet.
const LIVE_SEARCH_DEBOUNCE_MS = 300;
const LIVE_SEARCH_MIN_LENGTH = 2;

// Which chrome panels are visible (`prediction` is the collapsible bar inside
// the detail pane). Persisted (with the pane arrangement and sizes, see
// lib/layout.ts) so the operator's layout sticks.
// The shortcuts hint lives inside the sidebar, so it tracks `sidebar`.
export type Panels = { sidebar: boolean; detail: boolean; prediction: boolean };

// One read at module load; the states below fan out from it.
const INITIAL_UI = loadUi();

const OnboardingTour = lazy(
  () => import("@/components/console/OnboardingTour"),
);

// How long the "thread deleted · undo" window stays open before the delete is
// actually sent to the server.
const UNDO_MS = 5000;

// Guards the one-time OAuth code exchange. The authorization code is single-use,
// but React StrictMode invokes effects twice in dev — without this module-level
// latch the second run would re-POST the spent code and get `invalid_grant`.
let oauthExchangeStarted = false;

// Coarse on purpose: this only ever appears in a tooltip explaining why mail
// looks behind, where "3 hours ago" reads better than a timestamp.
function formatSyncTime(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms)) return "never";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${Math.max(1, minutes)}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export default function Console() {
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const pathname = window.location.pathname;
  const isEmailAuthScreen =
    pathname === "/auth/verify-email" || pathname === "/auth/reset-password";
  const isNarrow = useIsNarrow();
  const [narrowPane, setNarrowPane] = useState<
    "buckets" | "list" | "reading"
  >("list");

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
  const [firstListLoadSettled, setFirstListLoadSettled] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Scoped to a single connected account; null means "every account".
  const [accountFilter, setAccountFilter] = useState<string | null>(null);

  const [overview, setOverview] = useState<Overview | null>(null);
  const [refreshedHealth, setRefreshedHealth] = useState<SyncHealth | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [accountsOpen, setAccountsOpen] = useState(false);
  // Which OAuth providers this deployment has configured — gates "Connect
  // Outlook" everywhere it'd otherwise show up next to Gmail.
  const [authProviders, setAuthProviders] = useState<string[]>([]);
  const outlookEnabled = authProviders.includes("outlook");

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
  // Only "account" needs the server's cooperation (grouping is stable across
  // pages); confidence sort stays a client-side reorder of whatever's loaded.
  const serverSort: TriageSort = sortMode === "account" ? "account" : "recency";
  // Wide-layout-only multi-thread selection for batch triage (feature 8).
  const [bulkIds, setBulkIds] = useState<ReadonlySet<string>>(new Set());

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
  const [tourVersion, setTourVersion] = useState<number>(INITIAL_UI.tourVersion);
  const [density, setDensity] = useState<Density>(INITIAL_UI.density);
  const [fontScale, setFontScale] = useState<number>(INITIAL_UI.fontScale);
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
  const panelsRef = useRef(panels);
  panelsRef.current = panels;
  const itemsRef = useRef<TriageItem[]>([]);
  itemsRef.current = items;
  const accountFilterRef = useRef(accountFilter);
  accountFilterRef.current = accountFilter;
  const serverSortRef = useRef(serverSort);
  serverSortRef.current = serverSort;
  // Raw (pre-pendingDeletes-filter) server offset to fetch next; advances by
  // the full page length so deleted-but-still-server-side rows don't shift it.
  const nextOffsetRef = useRef(0);
  // Bumped on every page-0 reset so a loadMore response that resolves after a
  // newer reset started can be told apart from the page it thinks it's for.
  const pagingGenRef = useRef(0);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const liveSearchRef = useRef<LiveSearchController | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        UI_KEY,
        JSON.stringify({
          ...panels,
          arrangement,
          paneSizes,
          theme,
          autoSync,
          tourVersion,
          density,
          fontScale,
        }),
      );
    } catch {
      /* storage unavailable; layout just won't persist */
    }
  }, [panels, arrangement, paneSizes, theme, autoSync, tourVersion, density, fontScale]);

  const togglePanel = useCallback((key: keyof Panels) => {
    setPanels((p) => ({ ...p, [key]: !p[key] }));
  }, []);

  const showPanel = useCallback((key: keyof Panels) => {
    setPanels((current) =>
      current[key] ? current : { ...current, [key]: true },
    );
  }, []);

  const snapshotPanels = useCallback((): Panels => ({ ...panelsRef.current }), []);
  const restorePanels = useCallback((snapshot: Panels) => {
    setPanels(snapshot);
  }, []);

  const tourDeps = useMemo<TourDeps>(
    () => ({
      showPanel,
      setBucket,
      setIngestOpen,
      setAccountsOpen,
      snapshotPanels,
      restorePanels,
    }),
    [restorePanels, showPanel, snapshotPanels],
  );

  const {
    tourActive,
    stepIndex: tourStepIndex,
    targetResolution: tourTargetResolution,
    restartTour,
    deferTour,
    skipTour,
    finishTour,
    goToStep: goToTourStep,
    lockedPopover,
  } = useOnboardingTour({
    authChecked,
    hasUser: Boolean(user),
    tourVersion,
    firstListLoadSettled,
    narrowViewport: isNarrow,
    deps: tourDeps,
    setTourVersion,
  });

  useEffect(() => {
    // Defer, don't skip: shrinking mid-tour shouldn't burn tourVersion — the
    // tour picks back up when the viewport widens.
    if (isNarrow && tourActive) deferTour();
  }, [isNarrow, deferTour, tourActive]);

  const handlePaneSizes = useCallback((key: string, layout: PaneLayout) => {
    setPaneSizes((s) => ({ ...s, [key]: layout }));
  }, []);

  // ---- auth ----------------------------------------------------------------
  useEffect(() => {
    if (isEmailAuthScreen) return;
    (async () => {
      // The browser binding chooses the allowed callback endpoint and prevents
      // an OAuth response from a different tab from being exchanged here.
      if (
        window.location.pathname.endsWith("/auth/google/callback") &&
        !oauthExchangeStarted
      ) {
        oauthExchangeStarted = true;
        const params = new URLSearchParams(window.location.search);
        const code = params.get("code");
        const oauthError = params.get("error");
        const state = params.get("state");
        const binding = takeOauthBinding();
        if (!binding || binding.state !== state) {
          window.history.replaceState({}, "", "/");
          toast.error("sign-in was interrupted — try again");
        } else {
          try {
            if (oauthError || !code) {
              throw new Error("google sign-in was interrupted");
            }
            if (binding.mode === "login") {
              const res = await googleAuthCallback(code, state);
              setToken(res.access_token);
              setUser(res.user);
            } else {
              const res = await googleConnectCallback(code, state);
              toast.success(`${res.provider_email} connected — syncing`);
              try {
                setRefreshedHealth(await getSyncHealth());
              } catch {
                // The normal health poll will retry if this immediate refresh fails.
              }
              try {
                setConnections(await listConnections());
              } catch {
                // The post-auth effect refreshes connections too; not fatal here.
              }
            }
          } catch (e) {
            if (binding.mode === "connect" && e instanceof ApiError && e.status === 409) {
              toast.error(e.message);
            } else {
              toast.error(
                binding.mode === "connect"
                  ? "gmail connection failed"
                  : (e as Error).message || "google sign-in failed",
              );
            }
          } finally {
            window.history.replaceState({}, "", "/");
          }
        }
      }

      if (
        window.location.pathname.endsWith("/auth/microsoft/callback") &&
        !oauthExchangeStarted
      ) {
        oauthExchangeStarted = true;
        const params = new URLSearchParams(window.location.search);
        const code = params.get("code");
        const oauthError = params.get("error");
        const state = params.get("state");
        const binding = takeOauthBinding();
        if (!binding || binding.state !== state) {
          window.history.replaceState({}, "", "/");
          toast.error("sign-in was interrupted — try again");
        } else {
          try {
            if (oauthError || !code) {
              throw new Error("microsoft sign-in was interrupted");
            }
            if (binding.mode === "login") {
              const res = await microsoftAuthCallback(code, state);
              setToken(res.access_token);
              setUser(res.user);
            } else {
              const res = await microsoftConnectCallback(code, state);
              toast.success(`${res.provider_email} connected — syncing`);
              try {
                setRefreshedHealth(await getSyncHealth());
              } catch {
                // The normal health poll will retry if this immediate refresh fails.
              }
              try {
                setConnections(await listConnections());
              } catch {
                // The post-auth effect refreshes connections too; not fatal here.
              }
            }
          } catch (e) {
            if (binding.mode === "connect" && e instanceof ApiError && e.status === 409) {
              toast.error(e.message);
            } else {
              toast.error(
                binding.mode === "connect"
                  ? "outlook connection failed"
                  : (e as Error).message || "microsoft sign-in failed",
              );
            }
          } finally {
            window.history.replaceState({}, "", "/");
          }
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
  }, [isEmailAuthScreen]);

  const handleSessionExpired = useCallback(() => {
    setToken(null);
    setUser(null);
    toast.error("session expired — please sign in again");
  }, []);

  // ---- seen/unread (feature 9) ----------------------------------------------
  // In-memory per-user map; `seenVersion` is the render trigger since mutating
  // the ref itself doesn't re-render anything that reads it.
  const seenRef = useRef<Map<string, string>>(new Map());
  const [seenVersion, setSeenVersion] = useState(0);

  useEffect(() => {
    // Login/logout don't remount Console, so the map has to reload in place —
    // keyed on the id specifically (not the whole `user` object, which gets a
    // fresh reference on every getMe()) so this doesn't refire on unrelated
    // user refreshes.
    seenRef.current = user ? loadSeen(user.id) : new Map();
    setSeenVersion((v) => v + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  // ---- data fetching -------------------------------------------------------
  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await getOverview());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleSessionExpired();
    }
  }, [handleSessionExpired]);

  const refreshCounts = useCallback(async () => {
    // Server aggregates counts across the whole mailbox (scoped to the active
    // account filter, if any), so the sidebar totals don't cap at a single
    // triage page.
    try {
      setAllCounts(await getCounts(accountFilterRef.current));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleSessionExpired();
    }
  }, [handleSessionExpired]);

  const refreshConnections = useCallback(async () => {
    try {
      setConnections(await listConnections());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleSessionExpired();
    }
  }, [handleSessionExpired]);

  // The page-0 reset: always starts over at offset 0 under the current
  // sort/account filter (read from refs so this callback's identity — and
  // every effect keyed on it — doesn't change every time either one flips).
  const refreshList = useCallback(
    async (b: BucketKey, opts?: { quiet?: boolean }) => {
      const quiet = opts?.quiet ?? false;
      // Quiet refreshes (background sync) skip the loading flash and never
      // touch the selection — rows may shift, but the open thread stays open.
      if (!quiet) setListLoading(true);
      setListError(null);
      const generation = ++pagingGenRef.current;
      try {
        const res = await getTriage(b, PAGE_SIZE, {
          offset: 0,
          sort: serverSortRef.current,
          accountId: accountFilterRef.current,
        });
        if (generation !== pagingGenRef.current) return; // superseded by a newer reset
        const raw = res.items;
        // Rows mid-undo-window are already gone from the UI but not yet from
        // the server; a refresh must not resurrect them.
        const rows = raw.filter((i) => !pendingDeletes.current.has(i.thread_id));
        if (quiet && itemsRef.current.length > PAGE_SIZE) {
          // The operator has paged deep — a background refresh must not
          // collapse that back to one page. Update rows the fetch still
          // knows about in place, prepend anything genuinely new, and leave
          // the rest of the loaded tail untouched. This fetch only covers
          // page 0, so nextOffsetRef/hasMore must stay as they were -- setting
          // them from `raw` here would rewind pagination to page 0 and let
          // the load-more sentinel come back after the list was exhausted.
          const rowsById = new Map(rows.map((r) => [r.thread_id, r]));
          const existingIds = new Set(itemsRef.current.map((i) => i.thread_id));
          const freshIds = rows.filter((r) => !existingIds.has(r.thread_id));
          const updatedTail = itemsRef.current.map((i) => rowsById.get(i.thread_id) ?? i);
          setItems([...freshIds, ...updatedTail]);
        } else {
          nextOffsetRef.current = raw.length;
          setHasMore(raw.length === PAGE_SIZE);
          setItems(rows);
        }
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
        if (!quiet) {
          setListLoading(false);
          setFirstListLoadSettled(true);
        }
      }
    },
    [handleSessionExpired],
  );

  // Infinite scroll: fetches the next page at the server offset already
  // reached and appends it. Guarded against firing during a search, a
  // page-0 reset already underway, or while another page is loading.
  const loadMore = useCallback(async () => {
    if (!hasMore || loadingMore || listLoading || searchMode) return;
    const generation = pagingGenRef.current;
    setLoadingMore(true);
    try {
      const res = await getTriage(bucket, PAGE_SIZE, {
        offset: nextOffsetRef.current,
        sort: serverSortRef.current,
        accountId: accountFilterRef.current,
      });
      if (generation !== pagingGenRef.current) return; // a reset landed first — drop this page
      const raw = res.items;
      nextOffsetRef.current += raw.length;
      setHasMore(raw.length === PAGE_SIZE);
      const existingIds = new Set(itemsRef.current.map((i) => i.thread_id));
      const fresh = raw.filter(
        (i) => !pendingDeletes.current.has(i.thread_id) && !existingIds.has(i.thread_id),
      );
      setItems((prev) => [...prev, ...fresh]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        handleSessionExpired();
        return;
      }
      toast.error((e as Error).message ?? "failed to load more");
    } finally {
      setLoadingMore(false);
    }
  }, [hasMore, loadingMore, listLoading, searchMode, bucket, handleSessionExpired]);

  const refreshAll = useCallback(
    (opts?: { quiet?: boolean }) =>
      Promise.all([refreshList(bucket, opts), refreshOverview(), refreshCounts()]),
    [bucket, refreshList, refreshOverview, refreshCounts],
  );

  // Background sync is quiet periodic new-only ingest. After any sync that
  // changed the DB the whole console refreshes in place (no loading flash,
  // selection kept) and the "N new" pill re-derives from the persisted
  // acknowledged-mail watermark, so it survives reloads and syncs this tab
  // never saw finish. An empty mailbox pauses the loop entirely as there's no
  // baseline to sync against until the first manual ingest (which refreshes
  // the overview and thereby resumes it).
  const { pendingNew, clearNew, syncFailed, health } = useAutoSync({
    intervalSec: autoSync,
    enabled: !!user && (overview?.summary.threads ?? 0) > 0,
    busy: ingesting || backfilling,
    userId: user?.id ?? null,
    onSessionExpired: handleSessionExpired,
    onSynced: () => refreshAll({ quiet: true }).then(() => undefined),
  });

  useEffect(() => {
    setRefreshedHealth(null);
  }, [health]);

  const activeHealth = refreshedHealth ?? health;

  const handleConnectGmail = useCallback(async () => {
    try {
      const { auth_url } = await googleConnectStart();
      const state = extractStateFromAuthUrl(auth_url);
      if (state) saveOauthBinding({ mode: "connect", state, startedAt: Date.now() });
      window.location.href = auth_url;
    } catch (e) {
      toast.error((e as Error).message || "gmail connection unavailable");
    }
  }, []);

  const handleConnectOutlook = useCallback(async () => {
    try {
      const { auth_url } = await microsoftConnectStart();
      const state = extractStateFromAuthUrl(auth_url);
      if (state) saveOauthBinding({ mode: "connect", state, startedAt: Date.now() });
      window.location.href = auth_url;
    } catch (e) {
      toast.error((e as Error).message || "outlook connection unavailable");
    }
  }, []);

  // Picks the right OAuth flow for a broken connection's own provider —
  // reauth_required can hit a Gmail and an Outlook account at once, and each
  // needs its own reconnect action rather than one button assuming Gmail.
  const reconnectFor = useCallback(
    (provider: string) => (provider === "outlook" ? handleConnectOutlook : handleConnectGmail),
    [handleConnectOutlook, handleConnectGmail],
  );

  // Disconnecting deletes the account's synced mail server-side (cascade), so
  // the whole mailbox — not just the accounts list — needs to refresh after.
  const handleDisconnect = useCallback(
    async (connectionId: string) => {
      const provider = connections.find((c) => c.id === connectionId)?.provider;
      try {
        await deleteConnection(connectionId);
        toast.success(`${provider === "outlook" ? "outlook" : "gmail"} account disconnected`);
      } catch (e) {
        toast.error((e as Error).message ?? "disconnect failed");
        return;
      }
      await Promise.all([refreshConnections(), refreshAll()]);
      try {
        setRefreshedHealth(await getSyncHealth());
      } catch {
        // The normal health poll will retry if this immediate refresh fails.
      }
    },
    [connections, refreshConnections, refreshAll],
  );

  // What to say when mail isn't flowing. Ranked by what the operator can
  // actually do about it: reconnecting beats knowing the mailbox is behind,
  // and both beat "the scheduler is down" (which only we can fix).
  const syncStatus = useMemo((): {
    label: string;
    detail: string;
    actionable?: boolean;
    // Broken connections needing reauth, one per provider account — each
    // gets its own reconnect action since a Gmail account and an Outlook
    // account can both be broken at once, and one button can't relaunch
    // both OAuth flows.
    reconnectTargets?: Connection[];
    // Zero accounts connected: which providers can start one, when more
    // than just Gmail is available — same one-button-can't-cover-both-flows
    // reasoning as reconnectTargets, just before any account exists yet.
    connectProviders?: ("gmail" | "outlook")[];
  } | null => {
    if (activeHealth?.reason === "not_connected") {
      return outlookEnabled
        ? {
            label: "connect an account",
            detail: "No mail account is connected — connect Gmail or Outlook to start syncing.",
            actionable: true,
            connectProviders: ["gmail", "outlook"],
          }
        : {
            label: "connect gmail",
            detail: "No Gmail account is connected — connect one to start syncing.",
            actionable: true,
          };
    }
    if (activeHealth?.reason === "reauth_required") {
      const brokenIds = new Set(
        (activeHealth.accounts ?? [])
          .filter((a) => a.reason === "reauth_required")
          .map((a) => a.provider_account_id),
      );
      const broken = connections.filter((c) => brokenIds.has(c.id));
      // `connections` and the health poll are independent fetches — on a
      // fresh load, health can report reauth_required before `connections`
      // has resolved, joining to nothing here. Rendering this as actionable
      // would fall through to a fallback that assumes Gmail, which can
      // launch the wrong provider's OAuth flow for what's actually a broken
      // Outlook account. Stay inert until the join has something to act on;
      // the per-connection pills take over as soon as connections lands.
      if (broken.length === 0) {
        return {
          label: "reconnect required",
          detail: "An account needs reauthorization — reconnect to resume syncing.",
          actionable: false,
        };
      }
      const affected = broken.map((c) => c.email_address);
      const providerWord = (p: string) => (p === "outlook" ? "Outlook" : "Gmail");
      const providersInvolved = new Set(broken.map((c) => c.provider));
      // Mixed Gmail+Outlook breakage doesn't get to claim a single provider's
      // name in the summary line — the per-connection pills below already
      // say which is which.
      const subject = providersInvolved.size === 1 ? providerWord([...providersInvolved][0]) : "Account";
      // With more than one connected account, name which one needs a
      // reconnect instead of leaving the operator to guess.
      const detail =
        (activeHealth.accounts?.length ?? 0) > 1 && affected.length > 0
          ? `${subject} access was revoked for ${affected.join(", ")} — reconnect to resume syncing`
          : `${subject} access was revoked — reconnect the account to resume syncing`;
      return {
        label: broken.length > 1 ? "reconnect accounts" : `reconnect ${subject.toLowerCase()}`,
        detail,
        actionable: true,
        reconnectTargets: broken,
      };
    }
    if (activeHealth?.stale) {
      // A run being in flight softens the wording but never silences it: a
      // wedged sync retries for hours, so suppressing this while one is running
      // would hide the mailbox rotting behind it.
      const since = `no successful sync since ${formatSyncTime(activeHealth.last_succeeded_at)}`;
      return activeHealth.sync_in_progress
        ? { label: "syncing — mail is behind", detail: `${since}; a sync is running now` }
        : { label: "mail may be stale", detail: since };
    }
    if (activeHealth && !activeHealth.scheduler_alive) {
      return {
        label: "scheduler down",
        detail:
          "the background sync scheduler isn't checking in — this tab is keeping mail current for now",
      };
    }
    // Server says fine; fall back to what this tab has seen itself.
    if (syncFailed) {
      return {
        label: "sync failing",
        detail: "auto-sync is failing — retrying in the background",
      };
    }
    return null;
  }, [activeHealth, syncFailed, connections, outlookEnabled]);

  // initial + bucket changes. Switching buckets also exits any active search.
  // The new-mail pill deliberately survives bucket switches: it clears only on
  // explicit acknowledgment (pill click, `r`, manual ingest).
  useEffect(() => {
    if (!user) return;
    liveSearchRef.current?.cancel();
    setSearchMode(false);
    setSearchResults([]);
    setQuery("");
    refreshList(bucket);
  }, [user, bucket, refreshList]);

  useEffect(() => {
    if (!user) return;
    refreshOverview();
    refreshCounts();
    refreshConnections();
    listAuthProviders()
      .then(setAuthProviders)
      .catch(() => {
        // Connect Outlook is additive UI; a failed providers fetch just means
        // it stays hidden, not that the console itself is broken.
      });
  }, [user, refreshOverview, refreshCounts, refreshConnections]);

  // Account filter change: re-issue whatever's on screen under the new scope
  // — a live search stays a search (just re-run against the new account),
  // otherwise it's a paging reset — and the sidebar counts always follow.
  useEffect(() => {
    if (!user) return;
    if (searchMode) {
      const q = query.trim();
      if (q) liveSearchRef.current?.flush(q);
    } else {
      refreshList(bucket);
    }
    refreshCounts();
    // Only the filter itself should retrigger this — refreshList/refreshCounts
    // read the current filter via ref, and re-running on every render of
    // bucket/searchMode/query would refetch far more than the filter changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountFilter]);

  // Server-side sort change: only "account" vs "recency" actually changes
  // what the server returns, so this only fires crossing that boundary (a
  // confidence_asc <-> confidence_desc flip re-sorts client-side, no refetch).
  useEffect(() => {
    if (!user || searchMode) return;
    refreshList(bucket);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSort]);

  // A connection dropping out from under an active filter (disconnected mid-
  // session) falls back to "all accounts" instead of silently showing nothing.
  useEffect(() => {
    if (accountFilter && !connections.some((c) => c.id === accountFilter)) {
      setAccountFilter(null);
    }
  }, [connections, accountFilter]);

  // Bulk selection is scoped to the current view — switching what's on
  // screen leaves a stale selection dangling otherwise.
  useEffect(() => {
    setBulkIds(new Set());
  }, [bucket, accountFilter, searchMode]);

  useEffect(() => {
    if (isNarrow) setBulkIds(new Set());
  }, [isNarrow]);

  // Infinite scroll trigger: fires loadMore once the sentinel row scrolls
  // near the bottom of the list's own scroll container (not the viewport).
  useEffect(() => {
    if (searchMode || !hasMore) return;
    const root = listScrollRef.current;
    const target = sentinelRef.current;
    if (!root || !target) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadMore();
      },
      { root, rootMargin: "400px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, searchMode, loadMore]);

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
  // "recent" and "account" both come back already ordered from the server;
  // only the confidence sorts need a client-side reorder of the loaded page.
  const sortedItems = useMemo(() => {
    if (sortMode === "recent" || sortMode === "account") return items;
    const arr = [...items];
    arr.sort((a, b) => {
      const ac = a.classification.confidence ?? -1;
      const bc = b.classification.confidence ?? -1;
      return sortMode === "confidence_asc" ? ac - bc : bc - ac;
    });
    return arr;
  }, [items, sortMode]);

  // /mail/search has no sort param, so "account" grouping over search
  // results is purely a client-side reorder of whatever relevance order the
  // server returned — stable by account email, then most-recent-first.
  const sortedSearchResults = useMemo(() => {
    if (sortMode !== "account") return searchResults;
    return [...searchResults].sort((a, b) => {
      const byAccount = a.account_email.localeCompare(b.account_email);
      if (byAccount !== 0) return byAccount;
      const at = a.last_message_at ? Date.parse(a.last_message_at) : 0;
      const bt = b.last_message_at ? Date.parse(b.last_message_at) : 0;
      return bt - at;
    });
  }, [searchResults, sortMode]);

  // What the list actually shows: whole-mailbox search results when a search is
  // running, otherwise the bucket list with the instant client-side filter
  // applied on top.
  const visibleItems = useMemo(() => {
    if (searchMode) return sortedSearchResults;
    const q = query.trim().toLowerCase();
    if (!q) return sortedItems;
    return sortedItems.filter(
      (it) =>
        (it.subject ?? "").toLowerCase().includes(q) ||
        (it.latest_message_snippet ?? "").toLowerCase().includes(q),
    );
  }, [searchMode, sortedSearchResults, query, sortedItems]);

  const selectedIndex = useMemo(
    () => visibleItems.findIndex((i) => i.thread_id === selectedId),
    [visibleItems, selectedId],
  );

  const focusedItem = selectedIndex >= 0 ? visibleItems[selectedIndex] : null;

  // A selected-but-no-longer-visible row (a quiet background refresh replaced
  // it, or the client-side filter dropped it) must not stay batch-actionable.
  // Only writes when the intersection actually shrinks, so this can't loop.
  useEffect(() => {
    if (bulkIds.size === 0) return;
    const visibleIds = new Set(visibleItems.map((i) => i.thread_id));
    const pruned = new Set([...bulkIds].filter((id) => visibleIds.has(id)));
    if (pruned.size < bulkIds.size) setBulkIds(pruned);
  }, [visibleItems, bulkIds]);

  // ---- actions -------------------------------------------------------------
  const toggleBulk = useCallback((id: string) => {
    setBulkIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Batch hotkeys/buttons act on the selection reduced to what's actually
  // still on screen at the moment they fire.
  const batchTargets = useCallback(() => {
    const visibleIds = new Set(visibleItems.map((i) => i.thread_id));
    return [...bulkIds].filter((id) => visibleIds.has(id));
  }, [bulkIds, visibleItems]);

  const isUnseenFor = useCallback(
    (item: TriageItem) => isUnseen(seenRef.current, item.thread_id, item.last_message_at),
    // seenVersion bumping is the signal that seenRef.current changed; the ref
    // itself doesn't belong in the dep array.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [seenVersion],
  );

  // Marks a thread seen on explicit navigation only (row click, j/k, gg/G,
  // narrow Enter) — never on the automatic first-row selection after a load.
  const markThreadSeen = useCallback(
    (id: string) => {
      if (!user) return;
      const item =
        itemsRef.current.find((i) => i.thread_id === id) ??
        visibleItems.find((i) => i.thread_id === id);
      markSeen(seenRef.current, user.id, id, item?.last_message_at ?? null);
      setSeenVersion((v) => v + 1);
    },
    [user, visibleItems],
  );

  const moveSelection = useCallback(
    (delta: number) => {
      if (visibleItems.length === 0) return;
      const cur = selectedIndex < 0 ? 0 : selectedIndex;
      const next = Math.max(0, Math.min(visibleItems.length - 1, cur + delta));
      const target = visibleItems[next];
      setSelectedId(target.thread_id);
      markThreadSeen(target.thread_id);
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
    [visibleItems, selectedIndex, markThreadSeen],
  );

  // ---- search ----------------------------------------------------------
  // The actual fetch behind both live typing (debounced) and Enter (flushed);
  // createLiveSearch below owns the debounce/abort orchestration and calls
  // this once it decides a request should go out.
  const runLiveSearch = useCallback(
    async (q: string, signal: AbortSignal, fromFlush: boolean) => {
      setSearching(true);
      try {
        const res = await searchThreads(q, 200, { accountId: accountFilterRef.current, signal });
        const rows = res.items.filter((i) => !pendingDeletes.current.has(i.thread_id));
        setSearchResults(rows);
        setSearchMode(true);
        // Keep the current selection if it's still in the fresh results,
        // otherwise fall back to the first row — same rule Enter uses.
        setSelectedId((prev) =>
          prev && rows.some((i) => i.thread_id === prev) ? prev : (rows[0]?.thread_id ?? null),
        );
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return; // superseded, stay quiet
        if (e instanceof ApiError && e.status === 401) {
          handleSessionExpired();
          return;
        }
        if (e instanceof ApiError && e.status === 429) return; // rate-limited — keep showing what we have
        if (fromFlush) toast.error((e as Error).message ?? "search failed");
      } finally {
        // An aborted request's finally can still run after its successor
        // already flipped searching back on — only the request that's
        // actually still current gets to turn the spinner off.
        if (!signal.aborted) setSearching(false);
      }
    },
    [handleSessionExpired],
  );

  useEffect(() => {
    liveSearchRef.current = createLiveSearch({
      debounceMs: LIVE_SEARCH_DEBOUNCE_MS,
      minLength: LIVE_SEARCH_MIN_LENGTH,
      run: runLiveSearch,
      onBelowMin: () => {
        // Below the minimum length there's nothing worth asking the server —
        // drop back to the client-side filter over the loaded bucket.
        setSearchMode(false);
        setSearchResults([]);
      },
    });
    return () => liveSearchRef.current?.cancel();
  }, [runLiveSearch]);

  const clearSearch = useCallback(() => {
    liveSearchRef.current?.cancel();
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

  // Batch delete: mirrors doDelete's optimistic-remove + undo-window model,
  // just fanned out over every selected id under one shared timer (so the
  // pagehide flush above covers the whole batch for free).
  const doDeleteBatch = useCallback(
    (ids: string[]) => {
      if (ids.length === 0) return;
      const idSet = new Set(ids);

      const bucketSnapshot = new Map(
        ids
          .map((id) => [id, items.findIndex((i) => i.thread_id === id)] as const)
          .filter(([, idx]) => idx >= 0),
      );
      const removedFromBucket = new Map(
        [...bucketSnapshot.entries()].map(([id, idx]) => [id, items[idx]]),
      );
      const resultSnapshot = new Map(
        ids
          .map((id) => [id, searchResults.findIndex((i) => i.thread_id === id)] as const)
          .filter(([, idx]) => idx >= 0),
      );
      const removedFromResults = new Map(
        [...resultSnapshot.entries()].map(([id, idx]) => [id, searchResults[idx]]),
      );

      const prevSelectedId = selectedId;
      if (prevSelectedId && idSet.has(prevSelectedId)) {
        const remaining = visibleItems.filter((i) => !idSet.has(i.thread_id));
        setSelectedId(remaining[0]?.thread_id ?? null);
      }
      setItems((prev) => prev.filter((i) => !idSet.has(i.thread_id)));
      setSearchResults((prev) => prev.filter((i) => !idSet.has(i.thread_id)));
      setBulkIds(new Set());

      // Ascending index order so each splice doesn't shift a later insert.
      const restoreRows = (targetIds: string[]) => {
        const bucketOrder = targetIds
          .filter((id) => bucketSnapshot.has(id))
          .sort((a, b) => bucketSnapshot.get(a)! - bucketSnapshot.get(b)!);
        if (bucketOrder.length > 0) {
          setItems((prev) => {
            const copy = [...prev];
            for (const id of bucketOrder) {
              copy.splice(Math.min(bucketSnapshot.get(id)!, copy.length), 0, removedFromBucket.get(id)!);
            }
            return copy;
          });
        }
        const resultOrder = targetIds
          .filter((id) => resultSnapshot.has(id))
          .sort((a, b) => resultSnapshot.get(a)! - resultSnapshot.get(b)!);
        if (resultOrder.length > 0) {
          setSearchResults((prev) => {
            const copy = [...prev];
            for (const id of resultOrder) {
              copy.splice(Math.min(resultSnapshot.get(id)!, copy.length), 0, removedFromResults.get(id)!);
            }
            return copy;
          });
        }
      };

      const undo = () => {
        const timer = pendingDeletes.current.get(ids[0]);
        if (timer) clearTimeout(timer);
        for (const id of ids) pendingDeletes.current.delete(id);
        restoreRows(ids);
        if (prevSelectedId && idSet.has(prevSelectedId)) setSelectedId(prevSelectedId);
      };

      const timer = setTimeout(async () => {
        // A quiet background refresh racing this fan-out must keep masking
        // these ids until every delete call has actually settled — removing
        // them from pendingDeletes any earlier would let that refresh
        // resurrect a row mid-flight.
        const settled = await Promise.allSettled(ids.map((id) => deleteThread(id)));
        const failedIds = ids.filter((_, i) => settled[i].status === "rejected");
        for (const id of ids) pendingDeletes.current.delete(id);
        if (failedIds.length > 0) {
          restoreRows(failedIds);
          if (prevSelectedId && failedIds.includes(prevSelectedId)) {
            setSelectedId(prevSelectedId);
          }
          toast.error(`${failedIds.length} of ${ids.length} failed`);
        }
        refreshAll({ quiet: true });
      }, UNDO_MS);
      // Same timer under every id — the pagehide flush above walks
      // pendingDeletes entry-by-entry, so this covers the batch unchanged.
      for (const id of ids) pendingDeletes.current.set(id, timer);

      toast(`${ids.length} threads deleted`, {
        description: "removing in a few seconds",
        action: { label: "undo", onClick: undo },
        duration: UNDO_MS,
      });
    },
    [items, searchResults, selectedId, visibleItems, refreshAll],
  );

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
          setThread((prev) =>
            prev && prev.thread.id === id
              ? { ...prev, thread: { ...prev.thread, done: marking } }
              : prev,
          );
          refreshCounts();
          toast(marking ? "thread done" : "thread restored", {
            action: {
              label: "undo",
              onClick: () => {
                setThreadDone(id, !marking)
                  .then(() => {
                    setThread((prev) =>
                      prev && prev.thread.id === id
                        ? { ...prev, thread: { ...prev.thread, done: !marking } }
                        : prev,
                    );
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

  // Batch done/restore: mirrors doDone, fanned out with per-item failure
  // isolation — one thread rejecting the call doesn't undo the rest.
  const doDoneBatch = useCallback(
    (ids: string[]) => {
      if (ids.length === 0) return;
      const marking = bucket !== "done";
      const idSet = new Set(ids);

      const bucketSnapshot = new Map(
        ids
          .map((id) => [id, items.findIndex((i) => i.thread_id === id)] as const)
          .filter(([, idx]) => idx >= 0),
      );
      const removedFromBucket = new Map(
        [...bucketSnapshot.entries()].map(([id, idx]) => [id, items[idx]]),
      );

      // The bucket cache always drops done threads, search or not — search
      // mode just renders searchResults instead (doDoneBatch never touches
      // that list, so those rows stay put). Only the selection is
      // search-gated: it must not jump while browsing search results.
      if (!searchMode && selectedId && idSet.has(selectedId)) {
        const remaining = visibleItems.filter((i) => !idSet.has(i.thread_id));
        setSelectedId(remaining[0]?.thread_id ?? null);
      }
      setItems((prev) => prev.filter((i) => !idSet.has(i.thread_id)));
      setBulkIds(new Set());

      const restoreRows = (targetIds: string[]) => {
        const order = targetIds
          .filter((id) => bucketSnapshot.has(id))
          .sort((a, b) => bucketSnapshot.get(a)! - bucketSnapshot.get(b)!);
        if (order.length === 0) return;
        setItems((prev) => {
          const copy = [...prev];
          for (const id of order) {
            copy.splice(Math.min(bucketSnapshot.get(id)!, copy.length), 0, removedFromBucket.get(id)!);
          }
          return copy;
        });
      };

      const applyDoneState = (targetIds: string[], done: boolean) => {
        if (targetIds.length === 0) return;
        const targetSet = new Set(targetIds);
        setThread((prev) =>
          prev && targetSet.has(prev.thread.id)
            ? { ...prev, thread: { ...prev.thread, done } }
            : prev,
        );
      };

      void (async () => {
        const settled = await Promise.allSettled(ids.map((id) => setThreadDone(id, marking)));
        const succeededIds = ids.filter((_, i) => settled[i].status === "fulfilled");
        const failedIds = ids.filter((_, i) => settled[i].status === "rejected");

        if (failedIds.length > 0) {
          restoreRows(failedIds);
          toast.error(`${failedIds.length} of ${ids.length} failed`);
        }
        applyDoneState(succeededIds, marking);
        refreshCounts();

        if (succeededIds.length > 0) {
          toast(`${succeededIds.length} threads ${marking ? "done" : "restored"}`, {
            action: {
              label: "undo",
              onClick: () => {
                void Promise.allSettled(
                  succeededIds.map((id) => setThreadDone(id, !marking)),
                ).then((undone) => {
                  const restoredIds = succeededIds.filter((_, i) => undone[i].status === "fulfilled");
                  const undoFailedIds = succeededIds.filter((_, i) => undone[i].status === "rejected");
                  applyDoneState(restoredIds, !marking);
                  restoreRows(restoredIds);
                  if (undoFailedIds.length > 0) {
                    toast.error(
                      `undo failed for ${undoFailedIds.length} thread${undoFailedIds.length === 1 ? "" : "s"}`,
                    );
                  }
                  refreshCounts();
                });
              },
            },
          });
        }
      })();
    },
    [bucket, items, searchMode, selectedId, visibleItems, refreshCounts],
  );

  // Jump to the thread in the Gmail web UI (default signed-in account).
  const openInGmail = useCallback(() => {
    const t = thread?.thread;
    if (!t || t.id !== selectedId) return;
    if (t.provider !== "gmail" || !t.provider_thread_id) return;
    window.open(gmailThreadUrl(t.provider_thread_id, t.account_email), "_blank", "noopener");
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
        // One run per connected account now. Zero runs means nothing's
        // connected — not a failure, just nothing to do until the operator
        // connects a mail account.
        const runs = await ingestMail(
          opts.maxResults,
          opts.classify,
          opts.refreshExisting,
          false,
          opts.accountIds,
        );
        if (runs.length === 0) {
          toast.error(outlookEnabled ? "no email account connected" : "no Gmail account connected", {
            description: outlookEnabled
              ? "connect a Gmail or Outlook account to start syncing"
              : "connect one to start syncing",
            action: { label: "connect gmail", onClick: handleConnectGmail },
            ...(outlookEnabled
              ? { cancel: { label: "connect outlook", onClick: handleConnectOutlook } }
              : {}),
          });
          return;
        }
        // Every run landing deduplicated means some other caller (another
        // tab, auto-sync) already had every account covered — nothing new
        // was queued here, so this isn't "ingest complete".
        if (allRunsDeduplicated(runs)) {
          toast("another sync is already running — waiting for it");
        }
        const settled = await waitForSyncRuns(runs);
        const finals: SyncRunStatus[] = [];
        const failures: string[] = [];
        for (const s of settled) {
          if (s.status === "fulfilled") finals.push(s.value);
          else failures.push((s.reason as Error)?.message ?? "ingest failed");
        }
        for (const f of finals) {
          if (f.status === "failed" || f.result?.status === "error") {
            failures.push(f.result?.detail ?? f.error ?? "ingest failed");
          }
        }
        if (failures.length > 0) throw new Error(failures[0]);
        if (finals.some((f) => !f.ready)) {
          toast.message("ingest still running in the background — showing what's landed so far");
          await refreshAll({ quiet: true });
          return;
        }
        const { threads, messages } = sumIngestResults(finals);
        toast.success(`ingest complete · ${threads} threads · ${messages} msgs`);
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
    [refreshAll, clearNew, user, handleConnectGmail, handleConnectOutlook, outlookEnabled],
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

  // Batch reclassify: same optimistic-override-then-roll-back-on-failure
  // shape as doReclassify, `leavesBucket` computed once (it only depends on
  // bucket/label/searchMode, not on which thread).
  const doReclassifyBatch = useCallback(
    async (ids: string[], label: Label) => {
      if (ids.length === 0) return;
      const idSet = new Set(ids);
      const leavesBucket =
        !searchMode &&
        (bucket === "unclassified" ||
          (bucket !== "all" && bucket !== "done" && bucket !== label));

      const bucketSnapshot = new Map(
        ids
          .map((id) => [id, items.findIndex((i) => i.thread_id === id)] as const)
          .filter(([, idx]) => idx >= 0),
      );

      const prevSelectedId = selectedId;
      if (leavesBucket && prevSelectedId && idSet.has(prevSelectedId)) {
        const remaining = visibleItems.filter((i) => !idSet.has(i.thread_id));
        setSelectedId(remaining[0]?.thread_id ?? null);
      }

      // Snapshots captured from the updaters' live state (map runs inside the
      // setState updater), same idiom as the single-thread version above.
      const prevItems = new Map<string, TriageItem>();
      const prevResults = new Map<string, TriageItem>();
      const applyOverride = (it: TriageItem, capture: Map<string, TriageItem>): TriageItem => {
        if (!idSet.has(it.thread_id)) return it;
        capture.set(it.thread_id, it);
        return {
          ...it,
          classification: { label, confidence: 1, model_version: "user-override" },
        };
      };
      setItems((prev) => {
        const mapped = prev.map((it) => applyOverride(it, prevItems));
        return leavesBucket ? mapped.filter((it) => !idSet.has(it.thread_id)) : mapped;
      });
      setSearchResults((prev) => prev.map((it) => applyOverride(it, prevResults)));
      setBulkIds(new Set());

      const settled = await Promise.allSettled(ids.map((id) => reclassify(id, label)));
      const succeededIds = ids.filter((_, i) => settled[i].status === "fulfilled");
      const failedIds = ids.filter((_, i) => settled[i].status === "rejected");

      if (failedIds.length > 0) {
        const failedSet = new Set(failedIds);
        if (leavesBucket) {
          const order = failedIds
            .filter((id) => bucketSnapshot.has(id))
            .sort((a, b) => bucketSnapshot.get(a)! - bucketSnapshot.get(b)!);
          if (order.length > 0) {
            setItems((prev) => {
              const copy = [...prev];
              for (const id of order) {
                copy.splice(Math.min(bucketSnapshot.get(id)!, copy.length), 0, prevItems.get(id)!);
              }
              return copy;
            });
          }
        } else {
          setItems((prev) =>
            prev.map((it) => (failedSet.has(it.thread_id) ? (prevItems.get(it.thread_id) ?? it) : it)),
          );
        }
        setSearchResults((prev) =>
          prev.map((it) => (failedSet.has(it.thread_id) ? (prevResults.get(it.thread_id) ?? it) : it)),
        );
        if (leavesBucket && prevSelectedId && failedSet.has(prevSelectedId)) {
          setSelectedId(prevSelectedId);
        }
        toast.error(`${failedIds.length} of ${ids.length} failed`);
      }
      if (succeededIds.length > 0) {
        toast.success(`${succeededIds.length} → ${label}`);
      }
      refreshCounts();
    },
    [selectedId, bucket, items, searchMode, visibleItems, refreshCounts],
  );

  // ---- hotkeys -------------------------------------------------------------
  useHotkeys(
    (e) => {
      // overlays open: only handle escape
      if (shouldSuppressConsoleHotkeys(paletteOpen, shortcutsOpen, tourActive)) {
        if (e.key === "Escape") {
          if (tourActive) {
            skipTour();
          } else {
            setPaletteOpen(false);
            setShortcutsOpen(false);
          }
        }
        return;
      }

      // Escape priority: overlay/tour handling above already returned; a live
      // bulk selection is next, then search clears (works even while the
      // search box has focus, since useHotkeys lets Escape through).
      if (e.key === "Escape") {
        if (bulkIds.size > 0) {
          e.preventDefault();
          setBulkIds(new Set());
          return;
        }
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
        if (!isNarrow && bulkIds.size > 0) doDeleteBatch(batchTargets());
        else doDelete();
        return;
      }
      if (e.key === "e") {
        e.preventDefault();
        if (!isNarrow && bulkIds.size > 0) doDoneBatch(batchTargets());
        else doDone();
        return;
      }
      if (e.key === "o") {
        e.preventDefault();
        openInGmail();
        return;
      }
      // Toggles bulk-select on the focused row — wide layout only, mirrors
      // Gmail's `x` (no auto-advance). Collides with nothing else here.
      if (e.key === "x" && !isNarrow) {
        e.preventDefault();
        if (focusedItem) toggleBulk(focusedItem.thread_id);
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
        const label = ALL_LABELS[Number(e.key) - 1];
        if (!isNarrow && bulkIds.size > 0) doReclassifyBatch(batchTargets(), label);
        else doReclassify(label);
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
        if (visibleItems.length) {
          const target = visibleItems[visibleItems.length - 1];
          setSelectedId(target.thread_id);
          markThreadSeen(target.thread_id);
        }
      } else if (e.key === "g") {
        const now = Date.now();
        if (now - gPressedAt.current < 400) {
          if (visibleItems.length) {
            const target = visibleItems[0];
            setSelectedId(target.thread_id);
            markThreadSeen(target.thread_id);
          }
          gPressedAt.current = 0;
        } else {
          gPressedAt.current = now;
        }
      } else if (e.key === "Enter") {
        // already selected; this is a no-op besides ensuring scroll
        if (focusedItem) {
          if (isNarrow) {
            setNarrowPane("reading");
            markThreadSeen(focusedItem.thread_id);
          }
          document
            .querySelector(`[data-thread-row="${focusedItem.thread_id}"]`)
            ?.scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "c") {
        setSortMode(nextSortMode);
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
      tourActive,
      skipTour,
      searchMode,
      query,
      visibleItems,
      focusedItem,
      isNarrow,
      bucket,
      bulkIds,
      batchTargets,
      toggleBulk,
      moveSelection,
      markThreadSeen,
      clearSearch,
      togglePanel,
      doDelete,
      doDeleteBatch,
      doDone,
      doDoneBatch,
      doReclassify,
      doReclassifyBatch,
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
  // Every branch must render <Toaster> as the fragment's second child: sonner
  // drops toasts fired while no Toaster is mounted (it never replays them), and
  // keeping the same tree position preserves one instance across branch swaps.
  // The OAuth callback outcome toasts fire while the "checking session…" branch
  // is on screen, so losing it there made connect failures look like silent
  // bounces back to the console.
  if (pathname === "/auth/verify-email") {
    return (
      <>
        <VerifyEmailScreen
          onAuthed={(u) => {
            setUser(u);
            setAuthChecked(true);
          }}
        />
        <Toaster theme={resolvedTheme} />
      </>
    );
  }
  if (pathname === "/auth/reset-password") {
    return (
      <>
        <ResetPasswordScreen
          onAuthed={(u) => {
            setUser(u);
            setAuthChecked(true);
          }}
        />
        <Toaster theme={resolvedTheme} />
      </>
    );
  }
  if (!authChecked) {
    return (
      <>
        <div className="min-h-screen flex items-center justify-center text-muted-foreground font-mono text-sm">
          checking session…
        </div>
        <Toaster theme={resolvedTheme} />
      </>
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
        : sortMode === "confidence_desc"
          ? "conf ↓"
          : "account";

  // Account badges only earn their keep once there's something to
  // disambiguate — a single-account mailbox doesn't need them.
  const multiAccount = connections.length > 1;

  const sidebarPane = (
    <BucketSidebar
      active={bucket}
      counts={allCounts}
      onSelect={(b) => {
        setBucket(b);
        if (isNarrow) setNarrowPane("list");
      }}
      onCollapse={() => togglePanel("sidebar")}
      side={arrangement.sidebar}
      narrow={isNarrow}
    />
  );

  const listPane = (
    <section className="flex-1 min-w-0 min-h-0 flex flex-col">
      <div className="h-10 shrink-0 border-b border-border bg-[var(--color-panel)] panel-lift flex items-center px-3 gap-2.5 font-mono text-[11.5px]">
        <span className="text-primary font-semibold tracking-tight shrink-0">
          {searchMode ? "search" : bucket.replace("_", " ")}
        </span>
        {!isNarrow && bulkIds.size > 0 ? (
          <>
            <span className="text-muted-foreground tabular-nums shrink-0">
              {bulkIds.size} selected
            </span>
            <button
              onClick={() => doDoneBatch(batchTargets())}
              className="shrink-0 px-2 py-0.5 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              done
            </button>
            <button
              onClick={() => doDeleteBatch(batchTargets())}
              className="shrink-0 px-2 py-0.5 rounded border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20 cursor-pointer transition-colors"
            >
              delete
            </button>
            <button
              onClick={() => setBulkIds(new Set())}
              className="shrink-0 px-2 py-0.5 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              clear
            </button>
          </>
        ) : (
          <span className="text-muted-foreground tabular-nums shrink-0">
            {visibleItems.length}
            {searchMode ? " match" : " thread"}
            {visibleItems.length === 1 ? "" : "s"}
            {searchMode ? " · all buckets" : ""}
          </span>
        )}

        {pendingNew > 0 && (
          <button
            data-testid="new-mail-pill"
            onClick={() => {
              clearNew();
              refreshAll();
              listScrollRef.current?.scrollTo({ top: 0 });
            }}
            title="new mail — click to jump to the top"
            className="shrink-0 h-6 px-2 rounded-full border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20 tabular-nums cursor-pointer transition-colors animate-in fade-in-0 zoom-in-95 duration-150"
          >
            {pendingNew >= NEW_MAIL_SCAN_LIMIT ? `${NEW_MAIL_SCAN_LIMIT}+` : pendingNew} new
          </button>
        )}
        {syncStatus &&
          (syncStatus.reconnectTargets && syncStatus.reconnectTargets.length > 0 ? (
            // One pill per broken connection — a Gmail account and an
            // Outlook account can both need reauth at once, and each has to
            // relaunch its own provider's OAuth flow.
            syncStatus.reconnectTargets.map((c) => (
              <button
                key={c.id}
                data-testid="sync-failed-dot"
                onClick={reconnectFor(c.provider)}
                title={`${c.provider === "outlook" ? "Outlook" : "Gmail"} access was revoked for ${c.email_address} — reconnect to resume syncing`}
                className="shrink-0 h-6 px-2 rounded-full border border-amber-500/40 bg-amber-500/10 text-[11px] text-amber-700 dark:text-amber-300 hover:bg-amber-500/20 cursor-pointer transition-colors flex items-center gap-1"
              >
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500/70" />
                reconnect {emailLocalPart(c.email_address)}
              </button>
            ))
          ) : syncStatus.connectProviders && syncStatus.connectProviders.length > 0 ? (
            // Zero accounts connected and more than one provider is
            // configured — one pill per provider, same reasoning as the
            // reconnect pills above (one button can't launch two OAuth flows).
            syncStatus.connectProviders.map((p) => (
              <button
                key={p}
                data-testid="sync-failed-dot"
                onClick={p === "outlook" ? handleConnectOutlook : handleConnectGmail}
                title={syncStatus.detail}
                className="shrink-0 h-6 px-2 rounded-full border border-amber-500/40 bg-amber-500/10 text-[11px] text-amber-700 dark:text-amber-300 hover:bg-amber-500/20 cursor-pointer transition-colors flex items-center gap-1"
              >
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500/70" />
                connect {p}
              </button>
            ))
          ) : syncStatus.actionable ? (
            <button
              data-testid="sync-failed-dot"
              onClick={handleConnectGmail}
              title={syncStatus.detail}
              className="shrink-0 h-6 px-2 rounded-full border border-amber-500/40 bg-amber-500/10 text-[11px] text-amber-700 dark:text-amber-300 hover:bg-amber-500/20 cursor-pointer transition-colors flex items-center gap-1"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500/70" />
              {syncStatus.label}
            </button>
          ) : (
            <span
              data-testid="sync-failed-dot"
              title={syncStatus.detail}
              className="shrink-0 flex items-center gap-1 text-[11px] text-destructive/90"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-destructive/70" />
              {syncStatus.label}
            </span>
          ))}

        <div className="flex-1 flex items-center min-w-0 max-w-[380px] ml-1">
          <div className="flex items-center gap-1.5 w-full rounded border border-border bg-background px-2 h-6 focus-within:border-primary transition-colors">
            <Search className="h-3 w-3 text-muted-foreground shrink-0" />
            <input
              data-tour="search"
              aria-label="filter threads — press enter to search all buckets"
              ref={searchInputRef}
              value={query}
              onChange={(e) => {
                const v = e.target.value;
                setQuery(v);
                // Typing no longer exits searchMode itself — the live search
                // controller below decides: fresh results replace what's
                // showing, or (below minLength) onBelowMin drops back to the
                // client-side filter.
                liveSearchRef.current?.onInput(v);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  liveSearchRef.current?.flush(query);
                }
              }}
              placeholder="search…  ↵ to search all buckets"
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

        {multiAccount && (
          <select
            value={accountFilter ?? ""}
            onChange={(e) => setAccountFilter(e.target.value || null)}
            aria-label="Filter by account"
            className="shrink-0 px-2 py-0.5 rounded border border-border bg-transparent hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer transition-colors font-mono text-[11.5px]"
          >
            <option value="">all accounts</option>
            {connections.map((c) => (
              <option key={c.id} value={c.id}>
                {emailLocalPart(c.email_address)}
              </option>
            ))}
          </select>
        )}

        <button
          data-tour="sort"
          onClick={() => setSortMode(nextSortMode)}
          title="press c"
          aria-label={`Sort order: ${sortBadge}. Press c to cycle.`}
          className="shrink-0 px-2 py-0.5 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
        >
          sort: {sortBadge}
        </button>

        {!isNarrow && !panels.detail && (
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
      <div
        data-tour="thread-list"
        ref={listScrollRef}
        className="flex-1 overflow-y-auto scrollbar-thin"
      >
        {visibleItems.length === 0 &&
        activeHealth?.reason === "not_connected" &&
        !listLoading &&
        !searching &&
        !listError ? (
          <div className="h-full flex items-center justify-center p-6">
            <div className="w-full max-w-sm rounded-lg border border-border bg-[var(--color-panel)] p-6 text-center font-mono shadow-lg">
              <div className="mx-auto mb-3 h-10 w-10 rounded bg-primary/15 border border-primary/40 flex items-center justify-center">
                {outlookEnabled ? <Mail className="h-4 w-4 text-primary" /> : <GoogleMark />}
              </div>
              <div className="text-[13px] font-semibold text-foreground">
                {outlookEnabled
                  ? "Connect an account to start triaging"
                  : "Connect your Gmail to start triaging"}
              </div>
              <button
                type="button"
                onClick={handleConnectGmail}
                className="mt-5 w-full h-9 rounded border border-border bg-background font-mono text-[13px] font-semibold flex items-center justify-center gap-2 hover:bg-accent cursor-pointer transition-colors"
              >
                <GoogleMark />
                Connect Gmail
              </button>
              {outlookEnabled && (
                <button
                  type="button"
                  onClick={handleConnectOutlook}
                  className="mt-2 w-full h-9 rounded border border-border bg-background font-mono text-[13px] font-semibold flex items-center justify-center gap-2 hover:bg-accent cursor-pointer transition-colors"
                >
                  <MicrosoftMark />
                  Connect Outlook
                </button>
              )}
            </div>
          </div>
        ) : (
          <ThreadList
            items={visibleItems}
            selectedId={selectedId}
            onSelect={(id) => {
              setSelectedId(id);
              markThreadSeen(id);
              if (isNarrow) setNarrowPane("reading");
            }}
            showLabel={searchMode || bucket === "all" || bucket === "done"}
            showAccount={multiAccount}
            narrow={isNarrow}
            loading={listLoading || searching}
            error={listError}
            density={density}
            grouped={!searchMode && sortMode === "recent"}
            isUnseen={isUnseenFor}
            bulkIds={isNarrow ? undefined : bulkIds}
            onToggleBulk={isNarrow ? undefined : toggleBulk}
          />
        )}
        {!searchMode && hasMore && (
          <div
            ref={sentinelRef}
            className="h-10 flex items-center justify-center font-mono text-[11px] text-muted-foreground"
          >
            {loadingMore ? "loading more…" : ""}
          </div>
        )}
      </div>
    </section>
  );

  const detailPane = (
    <ThreadDetailPane
      data={thread}
      classification={focusedItem?.classification ?? null}
      loading={threadLoading}
      error={threadError}
      onReclassify={doReclassify}
      onBack={isNarrow ? () => setNarrowPane("list") : undefined}
      onCollapse={isNarrow ? undefined : () => togglePanel("detail")}
      onDone={focusedItem ? () => doDone(focusedItem.thread_id) : undefined}
      onDelete={focusedItem ? () => doDelete(focusedItem.thread_id) : undefined}
      showAccountBadge={multiAccount}
      side={arrangement.reading}
      predictionOpen={panels.prediction}
      onTogglePrediction={() => togglePanel("prediction")}
    />
  );

  return (
    <>
      {/* overflow-clip: no descendant (however hostile an email's CSS) may ever
          grow the page a scrollbar — panes own all scrolling. zoom (not
          root font-size) scales spacing along with text since row typography
          uses arbitrary-value px classes root font-size can't reach. Viewport
          units aren't zoom-compensated like auto/percentage sizes are, so the
          100vh height must be counter-divided or the console renders short
          (scale < 1) or overflows the screen (scale > 1). */}
      <div
        className="h-screen flex flex-col overflow-clip bg-background text-foreground"
        style={
          fontScale !== 1
            ? { zoom: fontScale, height: `calc(100vh / ${fontScale})` }
            : undefined
        }
      >
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
          density={density}
          onDensity={setDensity}
          fontScale={fontScale}
          onFontScale={setFontScale}
          theme={theme}
          onTheme={setTheme}
          autoSync={autoSync}
          onAutoSync={setAutoSync}
          connections={connections}
          health={activeHealth}
          accountsOpen={accountsOpen}
          onAccountsOpenChange={setAccountsOpen}
          onConnectGmail={handleConnectGmail}
          onConnectOutlook={outlookEnabled ? handleConnectOutlook : undefined}
          onDisconnect={handleDisconnect}
          ingestLocked={lockedPopover === "ingest"}
          accountsLocked={lockedPopover === "accounts"}
          onLogout={async () => {
            // Revoke server-side first, so the token is dead even if someone has
            // a copy of it. If that call fails we still clear locally — leaving
            // the user stuck in a session they asked to leave would be worse —
            // but we say so, because the token is still live out there.
            try {
              await revokeAllTokens();
            } catch {
              toast.error("signed out here, but couldn't revoke the session server-side");
            } finally {
              setToken(null);
              setUser(null);
            }
          }}
        />

        {isNarrow ? (
          <NarrowShell
            pane={narrowPane}
            onPaneChange={setNarrowPane}
            buckets={sidebarPane}
            list={listPane}
            reading={detailPane}
          />
        ) : (
          <ConsoleLayout
            arrangement={arrangement}
            onArrangementChange={setArrangement}
            sidebarVisible={panels.sidebar}
            detailVisible={panels.detail}
            onExpandSidebar={() => togglePanel("sidebar")}
            paneSizes={paneSizes}
            onPaneSizesChange={handlePaneSizes}
            sidebar={sidebarPane}
            list={listPane}
            detail={detailPane}
          />
        )}

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
          onRestartTour={restartTour}
        />
        <Shortcuts open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
        {!isNarrow && (tourActive || tourVersion < TOUR_VERSION) && (
          <Suspense fallback={null}>
            <OnboardingTour
              run={tourActive}
              stepIndex={tourStepIndex}
              targetResolution={tourTargetResolution}
              emptyThreadList={visibleItems.length === 0}
              emptyDetail={!thread}
              onStepChange={goToTourStep}
              onFinish={finishTour}
              onSkip={skipTour}
            />
          </Suspense>
        )}
      </div>
      <Toaster theme={resolvedTheme} />
    </>
  );
}
