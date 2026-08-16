import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Full mock, per the test harness rules -- mounting the signed-out branch
// pulls in LoginScreen (listAuthOptions) and, once a token resolves, the
// whole authenticated console's post-auth fetch effect. Most of those calls
// reject harmlessly (their callers already swallow non-401 errors); only
// getMe/getToken/setToken/revokeAllTokens are individually controlled below.
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
    getToken: vi.fn<() => string | null>(() => null),
    setToken: vi.fn<(t: string | null) => void>(() => {}),
    getMe: vi.fn<() => Promise<unknown>>(() => Promise.reject(new ApiError(401, "no token"))),
    revokeAllTokens: vi.fn<() => Promise<void>>(() => Promise.resolve()),
    listAuthProviders: vi.fn<() => Promise<string[]>>(() => Promise.resolve([])),
    listAuthOptions: vi.fn<() => Promise<{ providers: string[]; demoLogin: boolean }>>(
      () => Promise.resolve({ providers: [], demoLogin: false }),
    ),
    getOverview: vi.fn(notImplemented),
    getCounts: vi.fn(notImplemented),
    listConnections: vi.fn(notImplemented),
    getTriage: vi.fn(notImplemented),
    getActions: vi.fn(notImplemented),
    getSyncHealth: vi.fn(notImplemented),
    getLlmSettings: vi.fn(notImplemented),
    getLlmUsage: vi.fn(notImplemented),
    getClassifierMix: vi.fn(notImplemented),
    getThread: vi.fn(notImplemented),
    searchThreads: vi.fn(notImplemented),
    getActiveSync: vi.fn(notImplemented),
    // Never called in these tests (no ingest/backfill/reclassify/action is
    // ever triggered); present only so App.tsx's import list resolves.
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
    setActionStatus: vi.fn(notImplemented),
    setThreadDone: vi.fn(notImplemented),
    sumIngestResults: vi.fn(() => ({ ok: 0, failed: 0 })),
    testLlmSettings: vi.fn(notImplemented),
    waitForTask: vi.fn(notImplemented),
    waitForSyncRuns: vi.fn(() => Promise.resolve([])),
    WORKER_TASK_TIMEOUT_MS: 35 * 60 * 1000,
  };
});

vi.mock("@/lib/api", () => mockApi);

// Heavy console chrome, mocked so mounting the authenticated branch doesn't
// drag in real layout/keyboard/dialog machinery -- these tests only care
// which top-level screen (landing/login/console) is on the page.
vi.mock("@/components/console/TopBar", () => ({
  TopBar: (props: { onLogout: () => void }) => (
    <button type="button" data-testid="mock-logout" onClick={() => void props.onLogout()}>
      logout
    </button>
  ),
}));
vi.mock("@/components/console/OnboardingTour", () => ({
  default: () => null,
}));
// react-resizable-panels' PanelGroup needs a ResizeObserver, which jsdom
// doesn't implement -- these tests only need the console to mount, not lay
// its panes out, so the desktop split view is stubbed too.
vi.mock("@/components/console/ConsoleLayout", () => ({
  ConsoleLayout: (props: {
    sidebar: ReactNode;
    list: ReactNode;
    detail: ReactNode;
  }) => (
    <div data-testid="mock-console-layout">
      {props.sidebar}
      {props.list}
      {props.detail}
    </div>
  ),
  // BucketSidebar renders this inline as its own collapse/resize handle.
  PaneDragHandle: () => null,
}));
vi.mock("@/components/console/NarrowShell", () => ({
  NarrowShell: (props: {
    buckets: ReactNode;
    list: ReactNode;
    reading: ReactNode;
  }) => (
    <div data-testid="mock-narrow-shell">
      {props.buckets}
      {props.list}
      {props.reading}
    </div>
  ),
}));

import Console from "@/App";

let container: HTMLDivElement;
let root: Root;

function stubMatchMedia() {
  // useIsNarrow (App) needs matchMedia; report "wide" so the desktop
  // (ConsoleLayout) branch mounts instead of NarrowShell.
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
    // Flushes the auth-restore effect's microtasks (getToken snapshot ->
    // getMe -> setUser/setAuthChecked) so tests see the settled screen.
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function text() {
  return container.textContent ?? "";
}

function isLanding() {
  return text().includes("Your inbox, triaged by your own model.");
}

function isLoginScreen() {
  return container.querySelector("#login-email") !== null;
}

function isConsole() {
  return container.querySelector('[data-testid="mock-logout"]') !== null;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  stubMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  mockApi.getToken.mockReturnValue(null);
  mockApi.setToken.mockClear();
  mockApi.getMe.mockReset();
  mockApi.getMe.mockImplementation(() => Promise.reject(new mockApi.ApiError(401, "no token")));
  mockApi.revokeAllTokens.mockReset();
  mockApi.revokeAllTokens.mockImplementation(() => Promise.resolve());
  mockApi.listAuthProviders.mockClear();
  mockApi.listAuthProviders.mockImplementation(() => Promise.resolve([]));
  mockApi.listAuthOptions.mockClear();
  mockApi.listAuthOptions.mockImplementation(() =>
    Promise.resolve({ providers: [], demoLogin: false }),
  );
  // Console's post-auth fetch effect -- reject everything by default; each
  // caller already swallows non-401 errors, so this just keeps the
  // authenticated branch from crashing on an unimplemented call.
  for (const key of [
    "getOverview",
    "getCounts",
    "listConnections",
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

describe("App signed-out routing", () => {
  it("a tokenless visitor stays on the landing page", async () => {
    mockApi.getToken.mockReturnValue(null);
    mockApi.getMe.mockImplementation(() => Promise.reject(new mockApi.ApiError(401, "no token")));

    await renderApp();

    expect(isLanding()).toBe(true);
    expect(isLoginScreen()).toBe(false);
  });

  it("a valid token goes straight to the console -- landing never renders", async () => {
    mockApi.getToken.mockReturnValue("a-valid-token");
    mockApi.getMe.mockImplementation(() =>
      Promise.resolve({ id: "u1", email: "op@example.com", display_name: null }),
    );

    await renderApp();

    expect(isConsole()).toBe(true);
    expect(isLanding()).toBe(false);
    expect(isLoginScreen()).toBe(false);
  });

  it("clicking Sign in on the landing page switches to the login screen", async () => {
    mockApi.getToken.mockReturnValue(null);
    mockApi.getMe.mockImplementation(() => Promise.reject(new mockApi.ApiError(401, "no token")));

    await renderApp();
    expect(isLanding()).toBe(true);

    const signInButton = Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent?.trim() === "Sign in →",
    );
    expect(signInButton).toBeTruthy();

    await act(async () => {
      signInButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(isLoginScreen()).toBe(true);
    expect(isLanding()).toBe(false);
  });

  it("a rejected pre-existing token (401) sends the visitor to the login screen", async () => {
    mockApi.getToken.mockReturnValue("a-stale-token");
    mockApi.getMe.mockImplementation(() =>
      Promise.reject(new mockApi.ApiError(401, "token rejected")),
    );

    await renderApp();

    expect(isLoginScreen()).toBe(true);
    expect(isLanding()).toBe(false);
  });

  it("a restore network failure keeps the visitor's token but still lands on login", async () => {
    mockApi.getToken.mockReturnValue("a-token-thats-just-offline");
    mockApi.getMe.mockImplementation(() => Promise.reject(new Error("network unreachable")));

    await renderApp();

    expect(isLoginScreen()).toBe(true);
    expect(isLanding()).toBe(false);
  });

  it("a session expiring mid-session sends the operator back to the login screen", async () => {
    mockApi.getToken.mockReturnValue("a-valid-token");
    mockApi.getMe.mockImplementation(() =>
      Promise.resolve({ id: "u1", email: "op@example.com", display_name: null }),
    );
    // getOverview is the first post-auth call App fires; a 401 here drives
    // handleSessionExpired the same way any other console fetch would.
    mockApi.getOverview.mockImplementation(() =>
      Promise.reject(new mockApi.ApiError(401, "expired")),
    );

    await renderApp();
    // Let handleSessionExpired's state updates (setUser(null),
    // setSignedOutView("login")) settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(isLoginScreen()).toBe(true);
    expect(isConsole()).toBe(false);
    expect(isLanding()).toBe(false);
  });

  it("an OAuth login callback failure lands on the login screen, not the landing page", async () => {
    window.history.pushState({}, "", "/auth/google/callback?error=access_denied&state=xyz");
    // No saved binding for this state -- takeOauthBinding() returns null,
    // the "interrupted" branch fires.
    mockApi.getToken.mockReturnValue(null);
    mockApi.getMe.mockImplementation(() => Promise.reject(new mockApi.ApiError(401, "no token")));

    await renderApp();

    expect(isLoginScreen()).toBe(true);
    expect(isLanding()).toBe(false);

    window.history.pushState({}, "", "/");
  });

  it("logging out returns to the landing page", async () => {
    mockApi.getToken.mockReturnValue("a-valid-token");
    mockApi.getMe.mockImplementation(() =>
      Promise.resolve({ id: "u1", email: "op@example.com", display_name: null }),
    );

    await renderApp();
    expect(isConsole()).toBe(true);

    const logoutButton = container.querySelector<HTMLButtonElement>(
      '[data-testid="mock-logout"]',
    );
    expect(logoutButton).toBeTruthy();

    await act(async () => {
      logoutButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(isLanding()).toBe(true);
    expect(isConsole()).toBe(false);
  });

  it("LoginScreen's back control returns to the landing page", async () => {
    mockApi.getToken.mockReturnValue(null);
    mockApi.getMe.mockImplementation(() => Promise.reject(new mockApi.ApiError(401, "no token")));

    await renderApp();

    const signInButton = Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent?.trim() === "Sign in →",
    );
    await act(async () => {
      signInButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(isLoginScreen()).toBe(true);

    const backButton = Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent === "← Back to CortexMail",
    );
    expect(backButton).toBeTruthy();

    await act(async () => {
      backButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(isLanding()).toBe(true);
    expect(isLoginScreen()).toBe(false);
  });
});
