import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// use-auto-sync.ts calls the API module directly (no dependency injection),
// so exercising the reattach/interval code paths means mocking the module
// wholesale -- same pattern LabelSyncRow.render.test.tsx uses. Uses the same
// relative specifier ("./api") the hook itself imports, since that's the
// exact module vitest needs to intercept.
vi.mock("./api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  getActiveSync: vi.fn(),
  getSyncHealth: vi.fn(),
  getTriage: vi.fn(),
  ingestMail: vi.fn(),
  waitForSyncRuns: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    warning: vi.fn(),
    error: vi.fn(),
  },
}));

import { toast } from "sonner";
import { getActiveSync, getSyncHealth, getTriage, ingestMail, waitForSyncRuns } from "./api";
import type { SyncRunStatus } from "./api";
import { nextLeftUnclassifiedWarning, useAutoSync } from "./use-auto-sync";

function run(patch: Partial<SyncRunStatus> = {}): SyncRunStatus {
  return {
    run_id: "run-1",
    mode: "auto",
    status: "succeeded",
    ready: true,
    deduplicated: false,
    provider_account_id: "acct-1",
    result: { status: "ok" },
    ...patch,
  };
}

describe("nextLeftUnclassifiedWarning", () => {
  it("warns once when a run completes with mail left unclassified", () => {
    const first = nextLeftUnclassifiedWarning(
      [run({ result: { status: "ok", left_unclassified: 5 } })],
      false,
    );
    expect(first).toEqual({ total: 5, shouldWarn: true, warned: true });
  });

  it("does not warn again on a second consecutive run with mail still unclassified", () => {
    // alreadyWarned=true is what the first call above would have left the
    // caller's streak ref holding.
    const second = nextLeftUnclassifiedWarning(
      [run({ result: { status: "ok", left_unclassified: 5 } })],
      true,
    );
    expect(second).toEqual({ total: 5, shouldWarn: false, warned: true });
  });

  it("resets on a clean run, so a subsequent failing run warns again", () => {
    const clean = nextLeftUnclassifiedWarning(
      [run({ result: { status: "ok", left_unclassified: 0 } })],
      true,
    );
    expect(clean).toEqual({ total: 0, shouldWarn: false, warned: false });

    const failsAgain = nextLeftUnclassifiedWarning(
      [run({ result: { status: "ok", left_unclassified: 3 } })],
      clean.warned,
    );
    expect(failsAgain).toEqual({ total: 3, shouldWarn: true, warned: true });
  });

  it("treats a result missing left_unclassified as contributing 0, same as an older API", () => {
    const outcome = nextLeftUnclassifiedWarning([run({ result: { status: "ok" } })], false);
    expect(outcome).toEqual({ total: 0, shouldWarn: false, warned: false });
  });
});

// Integration-level coverage for the actual bug Codex flagged: the
// reload/reattach path (a sync already in flight when this tab mounted)
// has its own call site into the shared warn helper, and it's easy for
// that call site to silently go missing again without a test that mounts
// the real reattach code path instead of just the pure helper above.
describe("useAutoSync reattach path", () => {
  let root: Root;
  let container: HTMLElement;

  function Harness({ userId }: { userId: string | null }) {
    useAutoSync({
      intervalSec: 60,
      enabled: true,
      busy: false,
      userId,
      onSessionExpired: vi.fn(),
      onSynced: vi.fn(() => Promise.resolve()),
    });
    return null;
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    // Every mock (call history AND any resolved-value config from a prior
    // test) starts fresh -- getActiveSync/waitForSyncRuns in particular get
    // reconfigured per test below, and a stale mockResolvedValue from a
    // previous test would silently make this one pass for the wrong reason.
    vi.mocked(getActiveSync).mockReset();
    vi.mocked(waitForSyncRuns).mockReset();
    vi.mocked(getSyncHealth).mockResolvedValue({
      last_succeeded_at: null,
      stale: false,
      sync_in_progress: false,
      scheduler_alive: true,
      threshold_seconds: 1800,
      reason: null,
      accounts: [],
    });
    vi.mocked(getTriage).mockResolvedValue({ bucket: "all", items: [] });
    vi.mocked(ingestMail).mockResolvedValue([]);
    vi.mocked(toast.warning).mockClear();
    vi.mocked(toast.error).mockClear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.unstubAllGlobals();
  });

  // The reattach chain (getActiveSync -> waitForSyncRuns -> the warn/refresh
  // tail) is several real Promise hops deep, each only one microtask apart
  // but not synchronous -- a setTimeout(0) macrotask reliably drains all of
  // them, where counting a fixed number of `await Promise.resolve()` calls
  // would be guessing at the chain's exact depth.
  function flushPromises() {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }

  async function renderAndFlush(userId: string | null) {
    act(() => {
      root.render(<Harness userId={userId} />);
    });
    await act(async () => {
      await flushPromises();
      await flushPromises();
    });
  }

  it("warns about mail left unclassified by a run that was already in flight at mount", async () => {
    const inFlight = run({ result: { status: "ok", left_unclassified: 4 } });
    vi.mocked(getActiveSync).mockResolvedValue([inFlight]);
    vi.mocked(waitForSyncRuns).mockResolvedValue([{ status: "fulfilled", value: inFlight }]);

    await renderAndFlush("user-1");

    expect(toast.warning).toHaveBeenCalledTimes(1);
    expect(toast.warning).toHaveBeenCalledWith(
      "4 left unclassified — run backfill when your provider recovers",
    );
  });

  it("does not warn when the reattached run left nothing unclassified", async () => {
    const inFlight = run({ result: { status: "ok", left_unclassified: 0 } });
    vi.mocked(getActiveSync).mockResolvedValue([inFlight]);
    vi.mocked(waitForSyncRuns).mockResolvedValue([{ status: "fulfilled", value: inFlight }]);

    await renderAndFlush("user-1");

    expect(toast.warning).not.toHaveBeenCalled();
  });

  it("does nothing when nothing was in flight at mount", async () => {
    vi.mocked(getActiveSync).mockResolvedValue([]);

    await renderAndFlush("user-1");

    expect(waitForSyncRuns).not.toHaveBeenCalled();
    expect(toast.warning).not.toHaveBeenCalled();
  });
});

// CodeRabbit review on PR #53: Console keeps this hook mounted across
// logout/login, so without a reset, an account whose session ended with the
// streak ref already `warned: true` would silently suppress the NEXT
// account's first real warning. `enabled` here mirrors App.tsx's own
// `enabled: !!user && ...` gate -- a real logout always drops `enabled` to
// false before a different login can raise it again, so this exercises the
// exact same two-step transition the bug is about instead of a same-tick
// prop swap that would leave React's own effect-dependency diffing doing
// something less representative of the real app.
describe("useAutoSync per-account warning reset", () => {
  let root: Root;
  let container: HTMLElement;

  function Harness({ userId }: { userId: string | null }) {
    useAutoSync({
      intervalSec: 60,
      enabled: !!userId,
      busy: false,
      userId,
      onSessionExpired: vi.fn(),
      onSynced: vi.fn(() => Promise.resolve()),
    });
    return null;
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.mocked(getActiveSync).mockReset();
    vi.mocked(waitForSyncRuns).mockReset();
    vi.mocked(getSyncHealth).mockResolvedValue({
      last_succeeded_at: null,
      stale: false,
      sync_in_progress: false,
      scheduler_alive: true,
      threshold_seconds: 1800,
      reason: null,
      accounts: [],
    });
    vi.mocked(getTriage).mockResolvedValue({ bucket: "all", items: [] });
    vi.mocked(ingestMail).mockResolvedValue([]);
    vi.mocked(toast.warning).mockClear();
    vi.mocked(toast.error).mockClear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.unstubAllGlobals();
  });

  function flushPromises() {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }

  async function renderAndFlush(userId: string | null) {
    act(() => {
      root.render(<Harness userId={userId} />);
    });
    await act(async () => {
      await flushPromises();
      await flushPromises();
    });
  }

  it("warns for the next account even though the previous account's session already warned", async () => {
    const userAFlight = run({ result: { status: "ok", left_unclassified: 4 } });
    vi.mocked(getActiveSync).mockResolvedValueOnce([userAFlight]);
    vi.mocked(waitForSyncRuns).mockResolvedValueOnce([{ status: "fulfilled", value: userAFlight }]);

    await renderAndFlush("user-a");
    expect(toast.warning).toHaveBeenCalledTimes(1);
    expect(toast.warning).toHaveBeenNthCalledWith(
      1,
      "4 left unclassified — run backfill when your provider recovers",
    );

    // Logout -- `enabled` drops to false, same as Console's own gate. No
    // reattach call happens here (the cadence effect's guard clause returns
    // before ever reaching getActiveSync), so nothing needs mocking for
    // this step.
    await renderAndFlush(null);

    // A different account logs in, and its own first sync also leaves mail
    // unclassified. Without the reset, this would stay silent forever
    // because the ref would still read `warned: true` from user A.
    const userBFlight = run({ result: { status: "ok", left_unclassified: 6 } });
    vi.mocked(getActiveSync).mockResolvedValueOnce([userBFlight]);
    vi.mocked(waitForSyncRuns).mockResolvedValueOnce([{ status: "fulfilled", value: userBFlight }]);
    await renderAndFlush("user-b");

    expect(toast.warning).toHaveBeenCalledTimes(2);
    expect(toast.warning).toHaveBeenNthCalledWith(
      2,
      "6 left unclassified — run backfill when your provider recovers",
    );
  });
});
