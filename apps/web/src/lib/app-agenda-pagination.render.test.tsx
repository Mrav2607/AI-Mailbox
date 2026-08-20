import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionItem, ActionsResponse, ActionStatus } from "@/lib/types";

// Exercises App's own agenda pagination wiring (docs/plans/2026-08-19-
// agenda-cursor-pagination-plan.md, Wave 2) -- the cursor-walk in
// refreshActions, loadMoreActions' append/stale-drop behavior, and the
// agenda's own IntersectionObserver, all of which live entirely inside
// App's closures. Heavy chrome is mocked away (same pattern as
// app-settings-merge.render.test.tsx); BucketSidebar and AgendaList render
// for real so the test can drive them through actual DOM clicks and read
// real `data-action-row` output.
const mockApi = vi.hoisted(() => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, msg: string) {
      super(msg);
      this.status = status;
    }
  }
  const notImplemented = () => Promise.reject(new Error("not stubbed for this test"));
  return {
    ApiError,
    USE_MOCK: false,
    getToken: vi.fn<() => string | null>(() => "a-valid-token"),
    setToken: vi.fn<(t: string | null) => void>(() => {}),
    getMe: vi.fn<() => Promise<unknown>>(() =>
      Promise.resolve({ id: "u1", email: "alice@example.com", display_name: null }),
    ),
    revokeAllTokens: vi.fn<() => Promise<void>>(() => Promise.resolve()),
    listAuthProviders: vi.fn<() => Promise<string[]>>(() => Promise.resolve([])),
    getOverview: vi.fn(() =>
      Promise.resolve({ summary: { threads: 0, messages: 0, classified: 0 } }),
    ),
    getCounts: vi.fn(() =>
      Promise.resolve({
        counts: {
          needs_reply: 0,
          action_required: 0,
          fyi: 0,
          promotional: 0,
          security_alert: 0,
          spam: 0,
          all: 0,
          unclassified: 0,
          done: 0,
          snoozed: 0,
        },
        actions: { open: 0, overdue: 0 },
      }),
    ),
    listConnections: vi.fn(() => Promise.resolve([])),
    getTriage: vi.fn(() => Promise.resolve({ bucket: "needs_reply", items: [] })),
    getActions: vi.fn<
      (status?: ActionStatus, limit?: number, cursor?: string) => Promise<ActionsResponse>
    >(notImplemented),
    getSyncHealth: vi.fn(notImplemented),
    getLlmSettings: vi.fn(() =>
      Promise.resolve({
        configured: false,
        provider: null,
        model: null,
        base_url: null,
        key_suffix: null,
        last_verified_at: null,
        extraction_enabled: false,
        fallback_active: false,
        custom_endpoints_enabled: false,
        private_endpoints_enabled: false,
        custom_blocked: false,
        classification_byok: false,
        classification_fallback_local: false,
        classifier_uses_llm: false,
        classifier_backend: "heuristic",
        classification_eligible: false,
        classification_llm_usable: false,
      }),
    ),
    getLlmUsage: vi.fn(notImplemented),
    getClassifierMix: vi.fn(notImplemented),
    getThread: vi.fn(notImplemented),
    searchThreads: vi.fn(notImplemented),
    getActiveSync: vi.fn(notImplemented),
    allRunsDeduplicated: vi.fn(() => false),
    backfillActions: vi.fn(notImplemented),
    classifyBackfill: vi.fn(notImplemented),
    classifyQueue: vi.fn(notImplemented),
    deleteConnection: vi.fn(notImplemented),
    deleteLlmSettings: vi.fn(notImplemented),
    deleteThread: vi.fn(notImplemented),
    flushDeleteThread: vi.fn(() => {}),
    googleAuthCallback: vi.fn(notImplemented),
    googleConnectCallback: vi.fn(notImplemented),
    googleConnectStart: vi.fn(notImplemented),
    ingestMail: vi.fn(notImplemented),
    microsoftAuthCallback: vi.fn(notImplemented),
    microsoftConnectCallback: vi.fn(notImplemented),
    microsoftConnectStart: vi.fn(notImplemented),
    putLlmSettings: vi.fn(notImplemented),
    reclassify: vi.fn(notImplemented),
    setActionStatus: vi.fn<
      (id: string, status: ActionStatus) => Promise<{
        action_id: string;
        status: ActionStatus;
        status_at: string | null;
      }>
    >((id, status) => Promise.resolve({ action_id: id, status, status_at: null })),
    setThreadDone: vi.fn(notImplemented),
    snoozeThread: vi.fn(notImplemented),
    sumIngestResults: vi.fn(() => ({ threads: 0, messages: 0, leftUnclassified: 0 })),
    testLlmSettings: vi.fn(notImplemented),
    waitForTask: vi.fn(notImplemented),
    waitForSyncRuns: vi.fn(() => Promise.resolve([])),
    WORKER_TASK_TIMEOUT_MS: 35 * 60 * 1000,
  };
});

vi.mock("@/lib/api", () => mockApi);

vi.mock("@/components/console/TopBar", () => ({
  TopBar: () => <div data-testid="mock-topbar" />,
}));
vi.mock("@/components/console/OnboardingTour", () => ({
  default: () => null,
}));
vi.mock("@/components/console/SettingsDialog", () => ({
  SettingsDialog: () => <div data-testid="mock-settings-dialog" />,
}));
vi.mock("@/components/console/ConsoleLayout", () => ({
  ConsoleLayout: (props: {
    sidebar: React.ReactNode;
    list: React.ReactNode;
    detail: React.ReactNode;
  }) => (
    <div data-testid="mock-console-layout">
      {props.sidebar}
      {props.list}
      {props.detail}
    </div>
  ),
  PaneDragHandle: () => null,
}));
vi.mock("@/components/console/NarrowShell", () => ({
  NarrowShell: (props: {
    buckets: React.ReactNode;
    list: React.ReactNode;
    reading: React.ReactNode;
  }) => (
    <div data-testid="mock-narrow-shell">
      {props.buckets}
      {props.list}
      {props.reading}
    </div>
  ),
}));

import Console from "@/App";

// jsdom has no IntersectionObserver -- this stand-in records every instance
// (App constructs a separate one for triage and for the agenda) and its
// `root`/callback, so a test can pick out the agenda's own instance and fire
// its callback by hand instead of needing a real scroll gesture.
class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  root: Element | Document | null;
  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback;
    this.root = (options?.root as Element | Document | null) ?? null;
    FakeIntersectionObserver.instances.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

function makeAction(id: string, overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id,
    thread_id: `thread-${id}`,
    message_id: `${id}-m1`,
    kind: "other",
    title: `Action ${id}`,
    // Every generated row is deadline-less on purpose -- groupActions keeps
    // insertion order within a group untouched, so putting everything in
    // "no_deadline" makes DOM row order a direct, assertable proxy for
    // fetch/append order without fighting date-bucket placement.
    due_at: null,
    due_precision: null,
    due_raw: null,
    amount: null,
    currency: null,
    source_confidence: 0.9,
    status: "open",
    created_at: "2026-08-01T00:00:00Z",
    thread_subject: `Thread ${id}`,
    sender: "alice@example.com",
    provider: "gmail",
    account_email: "alice@gmail.com",
    label: "needs_reply",
    last_message_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

const PAGE1 = Array.from({ length: 100 }, (_, i) => makeAction(`act-${i}`));
const PAGE2 = Array.from({ length: 7 }, (_, i) => makeAction(`act-${100 + i}`));

// Keyed by the cursor a call arrives with (undefined for the first page) --
// stable across however many times the walk in refreshActions re-fetches the
// same page, unlike a call-count-based mock would be.
function pagedGetActions(pages: Record<string, ActionsResponse>) {
  return vi.fn(async (_status?: ActionStatus, _limit?: number, cursor?: string) => {
    const key = cursor ?? "start";
    const page = pages[key];
    if (!page) throw new Error(`no mock page registered for cursor ${JSON.stringify(key)}`);
    return page;
  });
}

const TWO_PAGE_RESPONSES: Record<string, ActionsResponse> = {
  start: { items: PAGE1, counts: { open: 107, overdue: 0 }, next_cursor: "c1" },
  c1: { items: PAGE2, counts: { open: 107, overdue: 0 }, next_cursor: null },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let container: HTMLDivElement;
let root: Root;

function stubMatchMedia() {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: true,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

async function renderApp() {
  await act(async () => {
    root.render(<Console />);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function goToAgenda() {
  const entry = container.querySelector<HTMLButtonElement>('[data-tour="agenda-entry"]')!;
  await act(async () => {
    entry.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function agendaRowIds(): string[] {
  return Array.from(container.querySelectorAll("[data-action-row]")).map((el) =>
    el.getAttribute("data-action-row"),
  ) as string[];
}

// The agenda's own observer -- the one rooted on the scroll container that
// wraps the `aria-label="agenda"` list, distinct from triage's (which never
// gets constructed here, since getTriage's stub never returns a full page).
function agendaObserver(): FakeIntersectionObserver {
  const agendaRoot = container.querySelector("ul[aria-label=\"agenda\"]")!.parentElement;
  const found = FakeIntersectionObserver.instances.find((o) => o.root === agendaRoot);
  if (!found) throw new Error("no IntersectionObserver rooted on the agenda's scroll container");
  return found;
}

async function fireIntersection(observer: FakeIntersectionObserver) {
  await act(async () => {
    observer.callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  FakeIntersectionObserver.instances = [];
  stubMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  mockApi.getActions.mockReset();
  mockApi.getTriage.mockClear();
  mockApi.setActionStatus.mockClear();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("agenda cursor pagination", () => {
  it("loadMoreActions appends the next page after the already-loaded rows, without reordering them", async () => {
    mockApi.getActions.mockImplementation(pagedGetActions(TWO_PAGE_RESPONSES));

    await renderApp();
    await goToAgenda();

    expect(agendaRowIds()).toEqual(PAGE1.map((a) => a.id));

    await fireIntersection(agendaObserver());

    expect(agendaRowIds()).toEqual([...PAGE1, ...PAGE2].map((a) => a.id));
    // Two calls total: the initial page and the one load-more page -- the
    // walk in refreshActions itself only fetched page one, since there was
    // nothing loaded yet to restore depth against.
    expect(mockApi.getActions).toHaveBeenCalledTimes(2);
  });

  it("roots its own observer on the agenda's scroll container and never drives the triage loader", async () => {
    mockApi.getActions.mockImplementation(pagedGetActions(TWO_PAGE_RESPONSES));

    await renderApp();
    await goToAgenda();
    const triageCallsBeforeIntersection = mockApi.getTriage.mock.calls.length;

    const observer = agendaObserver();
    const agendaContainer = container.querySelector("ul[aria-label=\"agenda\"]")!.parentElement;
    expect(observer.root).toBe(agendaContainer);

    await fireIntersection(observer);

    // The agenda page landed (loadMoreActions ran)...
    expect(agendaRowIds()).toHaveLength(107);
    // ...but nothing re-issued triage's own paged fetch.
    expect(mockApi.getTriage.mock.calls.length).toBe(triageCallsBeforeIntersection);
  });

  it("drops a load-more response that resolves after the generation bumps from a local mutation", async () => {
    const page2 = deferred<ActionsResponse>();
    mockApi.getActions.mockImplementation(async (_status, _limit, cursor) => {
      if (!cursor) {
        return { items: PAGE1, counts: { open: 107, overdue: 0 }, next_cursor: "c1" };
      }
      return page2.promise;
    });

    await renderApp();
    await goToAgenda();
    expect(agendaRowIds()).toHaveLength(100);

    // Starts loadMoreActions -- it's held open on the deferred page2 promise.
    const observer = agendaObserver();
    await act(async () => {
      observer.callback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        observer as unknown as IntersectionObserver,
      );
      await Promise.resolve();
    });

    // A local mutation (marking the first row done) bumps actionsGenRef
    // synchronously and removes that row optimistically -- the in-flight
    // load-more captured the OLD generation before this happened.
    const markDoneButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.getAttribute("aria-label") === `Mark "${PAGE1[0].title}" done`,
    )!;
    await act(async () => {
      markDoneButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(agendaRowIds()).toHaveLength(99);

    // The stale page finally resolves -- its 7 rows must never land.
    await act(async () => {
      page2.resolve({ items: PAGE2, counts: { open: 107, overdue: 0 }, next_cursor: null });
      await page2.promise;
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(agendaRowIds()).toHaveLength(99);
    expect(agendaRowIds()).not.toEqual(expect.arrayContaining(PAGE2.map((a) => a.id)));
  });

  it("holds the in-flight guard while a newer refresh walk is still running, even after a stale walk resolves", async () => {
    // Call 0: the agenda's first-ever load -- resolves right away and mints
    // a real cursor, so there's something a wrongly-unblocked load-more
    // could act on later. Calls 1 and 2 (walk A, then walk B) are left
    // pending so the test can resolve them in a controlled, out-of-order
    // sequence.
    const pendingResolvers: Record<number, (r: ActionsResponse) => void> = {};
    let callIndex = -1;
    mockApi.getActions.mockImplementation(async () => {
      callIndex += 1;
      if (callIndex === 0) {
        return { items: PAGE1, counts: { open: 100, overdue: 0 }, next_cursor: "c1" };
      }
      const d = deferred<ActionsResponse>();
      pendingResolvers[callIndex] = d.resolve;
      return d.promise;
    });

    await renderApp();
    await goToAgenda(); // call 0 -- settles immediately
    expect(agendaRowIds()).toHaveLength(100);

    const bucketEntry = Array.from(
      container.querySelectorAll<HTMLButtonElement>("nav button"),
    )[0]!;

    // Leave and come back: fires walk A (call 1), left pending.
    await act(async () => {
      bucketEntry.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    await goToAgenda();

    // Leave and come back again: fires walk B (call 2), also left pending,
    // and bumps actionsGenRef past walk A's -- from here on, A is stale.
    await act(async () => {
      bucketEntry.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    await goToAgenda();

    expect(mockApi.getActions).toHaveBeenCalledTimes(3);

    // Walk A (stale) resolves now, after walk B already started. Its own
    // finally block must not clear the in-flight guard walk B still holds --
    // that's the whole point of the generation check in the fix.
    await act(async () => {
      pendingResolvers[1]({ items: PAGE1, counts: { open: 100, overdue: 0 }, next_cursor: "c1" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // The board's cursor is still whatever call 0 minted -- a truthy value
    // loadMoreActions would act on immediately if the guard were (wrongly)
    // cleared by the stale walk. It must stay a no-op while walk B is
    // still outstanding.
    await fireIntersection(agendaObserver());
    expect(mockApi.getActions).toHaveBeenCalledTimes(3);

    // Walk B finally resolves -- everything settles normally, and the guard
    // releases for real once its own generation is the current one.
    await act(async () => {
      pendingResolvers[2]({ items: PAGE1, counts: { open: 100, overdue: 0 }, next_cursor: "c1" });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(agendaRowIds()).toHaveLength(100);

    // With the guard properly released, a load-more now goes through.
    await fireIntersection(agendaObserver());
    expect(mockApi.getActions).toHaveBeenCalledTimes(4);
  });

  it("suppresses a second observer fire that arrives before the first load-more's state update flushes", async () => {
    const page2 = deferred<ActionsResponse>();
    mockApi.getActions.mockImplementation(async (_status, _limit, cursor) => {
      if (!cursor) {
        return { items: PAGE1, counts: { open: 107, overdue: 0 }, next_cursor: "c1" };
      }
      return page2.promise;
    });

    await renderApp();
    await goToAgenda();
    expect(agendaRowIds()).toHaveLength(100);

    const observer = agendaObserver();
    // Two intersection callbacks fire back-to-back, synchronously -- before
    // React's setActionsLoadingMore(true) from the first call has any
    // chance to flush into a rerender. Only a synchronous ref (not the
    // actionsLoadingMore state) can stop the second from double-fetching.
    await act(async () => {
      observer.callback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        observer as unknown as IntersectionObserver,
      );
      observer.callback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        observer as unknown as IntersectionObserver,
      );
      await Promise.resolve();
    });

    // Page one, plus exactly ONE load-more fetch -- the second callback's
    // call must have no-op'd against the in-flight ref rather than issuing
    // its own request against the same cursor.
    expect(mockApi.getActions).toHaveBeenCalledTimes(2);

    await act(async () => {
      page2.resolve({ items: PAGE2, counts: { open: 107, overdue: 0 }, next_cursor: null });
      await page2.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    // Exactly one copy of page two landed -- no duplicate rows from a
    // second in-flight fetch racing the first's append.
    expect(agendaRowIds()).toEqual([...PAGE1, ...PAGE2].map((a) => a.id));
  });

  it("a fresh refetch after scrolling multiple pages deep walks the cursor forward and restores the same depth", async () => {
    mockApi.getActions.mockImplementation(pagedGetActions(TWO_PAGE_RESPONSES));

    await renderApp();
    await goToAgenda();
    await fireIntersection(agendaObserver());
    expect(agendaRowIds()).toHaveLength(107);
    const callsSoFar = mockApi.getActions.mock.calls.length;

    // Leave the agenda and come back -- App's own effect re-fires
    // refreshActions() from scratch on the view transition, exercising the
    // exact same cursor-walk a background refresh would run.
    const bucketEntry = Array.from(
      container.querySelectorAll<HTMLButtonElement>("nav button"),
    )[0]!;
    await act(async () => {
      bucketEntry.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    await goToAgenda();

    // Walked both pages again (not just page one) -- a naive single-page
    // refresh would have collapsed the board back down to 100 rows.
    expect(mockApi.getActions.mock.calls.length).toBe(callsSoFar + 2);
    expect(agendaRowIds()).toHaveLength(107);
  });
});
