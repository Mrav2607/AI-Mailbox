import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Connection } from "@/lib/types";

// The fenced merge (settings-card plan §3.2/P1-1) lives entirely inside
// App's own closures (connectionsGenRef, userIdRef, handleConnectionUpdated),
// so exercising it needs App actually mounted -- a unit test on an extracted
// helper wouldn't cover the ordering half, which depends on
// refreshConnections' own generation guard reacting to the bump. Heavy
// chrome is mocked away (same pattern as app-signed-out.render.test.tsx);
// SettingsDialog is mocked down to a prop-capturing stub so the test can
// call onConnectionUpdated/onTabChange directly instead of driving Radix
// through real DOM clicks.
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
    getOverview: vi.fn(notImplemented),
    getCounts: vi.fn(notImplemented),
    listConnections: vi.fn<() => Promise<Connection[]>>(notImplemented),
    deleteConnection: vi.fn<() => Promise<void>>(() => Promise.resolve()),
    getTriage: vi.fn(notImplemented),
    getActions: vi.fn(notImplemented),
    getSyncHealth: vi.fn(notImplemented),
    getLlmSettings: vi.fn(notImplemented),
    getLlmUsage: vi.fn(notImplemented),
    getClassifierMix: vi.fn(notImplemented),
    getThread: vi.fn(notImplemented),
    searchThreads: vi.fn(notImplemented),
    getActiveSync: vi.fn(notImplemented),
    allRunsDeduplicated: vi.fn(() => false),
    backfillActions: vi.fn(notImplemented),
    classifyBackfill: vi.fn(notImplemented),
    classifyQueue: vi.fn(notImplemented),
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
    setActionStatus: vi.fn(notImplemented),
    setThreadDone: vi.fn(notImplemented),
    snoozeThread: vi.fn(notImplemented),
    sumIngestResults: vi.fn(() => ({ threads: 0, messages: 0 })),
    testLlmSettings: vi.fn(notImplemented),
    waitForTask: vi.fn(notImplemented),
    waitForSyncRuns: vi.fn(() => Promise.resolve([])),
    WORKER_TASK_TIMEOUT_MS: 35 * 60 * 1000,
  };
});

vi.mock("@/lib/api", () => mockApi);

vi.mock("@/components/console/TopBar", () => ({
  TopBar: (props: { onLogout: () => void }) => (
    <button type="button" data-testid="mock-logout" onClick={() => void props.onLogout()}>
      logout
    </button>
  ),
}));

// Captures the SettingsDialog's latest props so the test can call
// onConnectionUpdated/onTabChange directly -- the same seam a real
// LabelSyncRow's PATCH response or a real tab click would drive, without
// needing Radix's actual DOM.
const settingsCapture = vi.hoisted(() => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  current: null as any,
}));
vi.mock("@/components/console/SettingsDialog", () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  SettingsDialog: (props: any) => {
    settingsCapture.current = props;
    return <div data-testid="mock-settings-dialog" />;
  },
}));

const loginCapture = vi.hoisted(() => ({
  current: null as ((u: { id: string; email: string; display_name: string | null }) => void) | null,
}));
vi.mock("@/components/console/LoginScreen", () => ({
  LoginScreen: (props: {
    onAuthed: (u: { id: string; email: string; display_name: string | null }) => void;
  }) => {
    loginCapture.current = props.onAuthed;
    return <div data-testid="mock-login-screen" />;
  },
}));

vi.mock("@/components/console/OnboardingTour", () => ({
  default: () => null,
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

function makeConnection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: "conn-1",
    provider: "gmail",
    email_address: "alice@gmail.com",
    created_at: "2026-08-01T00:00:00Z",
    reauth_required: false,
    label_sync_enabled: true,
    label_sync_drift: 5,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
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

function findConnection(id: string): Connection | undefined {
  return (settingsCapture.current?.connections as Connection[] | undefined)?.find(
    (c) => c.id === id,
  );
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  stubMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  settingsCapture.current = null;
  loginCapture.current = null;

  mockApi.getToken.mockReturnValue("a-valid-token");
  mockApi.getMe.mockReset();
  mockApi.getMe.mockImplementation(() =>
    Promise.resolve({ id: "u1", email: "alice@example.com", display_name: null }),
  );
  mockApi.revokeAllTokens.mockReset();
  mockApi.revokeAllTokens.mockImplementation(() => Promise.resolve());
  mockApi.listAuthProviders.mockClear();
  mockApi.listAuthProviders.mockImplementation(() => Promise.resolve([]));
  mockApi.deleteConnection.mockReset();
  mockApi.deleteConnection.mockImplementation(() => Promise.resolve());
  for (const key of [
    "getOverview",
    "getCounts",
    "getTriage",
    "getActions",
    "getSyncHealth",
    "getLlmSettings",
    "getLlmUsage",
    "getClassifierMix",
    "getThread",
    "searchThreads",
    "getActiveSync",
  ] as const) {
    (mockApi[key] as ReturnType<typeof vi.fn>).mockReset();
    (mockApi[key] as ReturnType<typeof vi.fn>).mockImplementation(() =>
      Promise.reject(new Error("not stubbed for this test")),
    );
  }
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("App's fenced connection-update merge", () => {
  it("a GET that started before a PATCH and resolves after it is discarded, not applied over the merge", async () => {
    const connA = makeConnection();
    const secondGet = deferred<Connection[]>();
    let getCall = 0;
    mockApi.listConnections.mockImplementation(() => {
      getCall += 1;
      // The mount effect's own refreshConnections() -- resolves right away
      // so the popover/dialog has a starting connection to merge into.
      if (getCall === 1) return Promise.resolve([connA]);
      // handleDisconnect's refreshConnections() below -- held open so it can
      // resolve AFTER the merge that follows.
      return secondGet.promise;
    });

    await renderApp();
    expect(findConnection("conn-1")?.label_sync_drift).toBe(5);

    // Fires App's real handleDisconnect, which calls the real
    // refreshConnections() -- capturing connectionsGenRef's current
    // generation before the merge below bumps it. deleteConnection is
    // stubbed to succeed instantly; only the listConnections() GET it
    // triggers stays in flight.
    await act(async () => {
      void settingsCapture.current.onDisconnect("conn-1");
      await Promise.resolve();
    });

    // A PATCH (LabelSyncRow's real update path, simulated here by calling
    // the prop directly) resolves while that GET is still pending, turning
    // off drift.
    act(() => {
      settingsCapture.current.onConnectionUpdated(
        makeConnection({ label_sync_enabled: true, label_sync_drift: 0 }),
      );
    });
    expect(findConnection("conn-1")?.label_sync_drift).toBe(0);

    // The stale GET finally resolves, carrying the OLD drift count. Without
    // the ordering fix (bumping connectionsGenRef before the merge above),
    // this would win and put drift back to 5.
    await act(async () => {
      secondGet.resolve([connA]); // stale: drift still 5
      await secondGet.promise.catch(() => {});
    });

    expect(findConnection("conn-1")?.label_sync_drift).toBe(0);
  });

  it("a PATCH resolving after the signed-in account changes is dropped, not merged into the new account's list", async () => {
    mockApi.listConnections.mockImplementation(() => Promise.resolve([makeConnection()]));

    await renderApp();
    expect(findConnection("conn-1")).toBeDefined();

    // Capture the update callback while user u1 is still signed in -- this
    // is the same closure instance a real LabelSyncRow would be holding if
    // its PATCH had started right now.
    const staleUpdateFn = settingsCapture.current.onConnectionUpdated;

    // Sign out, then in as a different account -- both go through App's
    // real state transitions (onLogout, then LoginScreen's onAuthed).
    const logoutButton = container.querySelector<HTMLButtonElement>(
      '[data-testid="mock-logout"]',
    );
    await act(async () => {
      logoutButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    const signInButton = Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent?.trim() === "Sign in →",
    );
    await act(async () => {
      signInButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    // u2 happens to have its own row that reuses the same connection id --
    // an edge case for a real UUID, but the sharpest way to prove the
    // ownership guard actually fires: a bare `.map`-by-id merge (no guard)
    // would silently overwrite u2's own data with u1's stale PATCH here,
    // where a merge into an empty list wouldn't show anything either way.
    const u2Connection = makeConnection({
      email_address: "bob@outlook.com",
      label_sync_enabled: false,
      label_sync_drift: null,
    });
    mockApi.listConnections.mockImplementation(() => Promise.resolve([u2Connection]));
    await act(async () => {
      loginCapture.current!({ id: "u2", email: "bob@example.com", display_name: null });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(findConnection("conn-1")?.email_address).toBe("bob@outlook.com");

    // The PATCH from u1's session finally resolves now that u2 is signed in.
    act(() => {
      staleUpdateFn(makeConnection({ label_sync_enabled: true, label_sync_drift: 0 }));
    });

    // Still u2's own row, untouched -- u1's stale update never got merged in.
    expect(findConnection("conn-1")?.email_address).toBe("bob@outlook.com");
    expect(findConnection("conn-1")?.label_sync_enabled).toBe(false);
  });
});

describe("the settings dialog on user change", () => {
  it("doesn't reopen the previous user's tab on the next login (migrated from use-llm-panel's own test)", async () => {
    // Login/logout keeps the Console component instance mounted (App.tsx's
    // own comment on this), so `settingsPanel` state genuinely survives the
    // trip through the signed-out screens unless something resets it --
    // that's the bug this test is actually guarding against, not whether the
    // dialog element itself is on screen (it isn't, either way, while
    // signed out: the whole authenticated branch unmounts on its own).
    mockApi.listConnections.mockImplementation(() => Promise.resolve([]));
    await renderApp();

    // Stands in for any entry point opening the dialog -- they all funnel
    // through this same tab-change callback (settings-card plan §3.3).
    act(() => {
      settingsCapture.current.onTabChange("ai");
    });
    expect(settingsCapture.current.open).toBe(true);
    settingsCapture.current = null;

    const logoutButton = container.querySelector<HTMLButtonElement>(
      '[data-testid="mock-logout"]',
    );
    await act(async () => {
      logoutButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    const signInButton = Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent?.trim() === "Sign in →",
    );
    await act(async () => {
      signInButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await act(async () => {
      loginCapture.current!({ id: "u2", email: "bob@example.com", display_name: null });
      await Promise.resolve();
      await Promise.resolve();
    });

    // SettingsDialog just remounted for the new account -- without App's own
    // reset (settingsPanel -> null alongside its other account-scoped
    // resets), this would render `open: true` on the very first frame.
    expect(settingsCapture.current.open).toBe(false);
  });
});
