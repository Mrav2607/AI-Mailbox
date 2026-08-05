import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError } from "@/lib/api";
import type {
  ClassifierMixEntry,
  LlmProvider,
  LlmSettings,
  LlmTestResult,
  LlmUsage,
} from "@/lib/types";
import { useLlmPanel, type LlmPanelDeps } from "@/lib/use-llm-panel";

// A promise this test controls the settlement of, so it can interleave
// "call the hook" with "resolve the fetch" in whatever order a bug would
// actually get exercised in the browser.
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeSettings(overrides: Partial<LlmSettings> = {}): LlmSettings {
  return {
    configured: true,
    provider: "openai",
    model: "gpt-4o-mini",
    base_url: null,
    key_suffix: "abcd",
    last_verified_at: null,
    extraction_enabled: true,
    fallback_active: false,
    custom_endpoints_enabled: true,
    private_endpoints_enabled: true,
    custom_blocked: false,
    classification_byok: false,
    classifier_uses_llm: true,
    classifier_backend: "heuristic",
    classification_eligible: true,
    classification_llm_usable: true,
    ...overrides,
  };
}

function makeUsage(overrides: Partial<LlmUsage> = {}): LlmUsage {
  return {
    window_days: 30,
    totals: {
      calls: 0,
      calls_with_total_tokens: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
    },
    by_stage: [],
    by_provider: [],
    daily: [],
    ...overrides,
  };
}

const MIX: ClassifierMixEntry[] = [{ kind: "local", count: 3 }];

describe("useLlmPanel rendered lifecycle", () => {
  let root: Root;
  let container: HTMLElement;
  let deps: LlmPanelDeps;
  let api: ReturnType<typeof useLlmPanel> | null;
  let onSessionExpired: Mock<() => void>;
  let toastSuccess: Mock<(message: string) => void>;
  let toastError: Mock<(message: string) => void>;

  function Harness({ userId }: { userId: string | null }) {
    api = useLlmPanel({ userId, deps });
    return null;
  }

  function render(userId: string | null) {
    act(() => {
      root.render(<Harness userId={userId} />);
    });
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    onSessionExpired = vi.fn<() => void>();
    toastSuccess = vi.fn<(message: string) => void>();
    toastError = vi.fn<(message: string) => void>();
    // Individual tests overwrite whichever of these they need to control;
    // the rest just resolve to something harmless so an unrelated call
    // (e.g. openLlmSettings' own usage/mix side-fetches) doesn't hang.
    deps = {
      getLlmSettings: vi.fn(() => Promise.resolve(makeSettings())),
      putLlmSettings: vi.fn(
        (input: {
          provider: LlmProvider;
          api_key?: string;
          model: string;
          base_url?: string;
          classification_byok?: boolean;
        }) => Promise.resolve(makeSettings({ provider: input.provider, model: input.model })),
      ),
      testLlmSettings: vi.fn(() =>
        Promise.resolve<LlmTestResult>({ ok: true, latency_ms: 12, error: null }),
      ),
      deleteLlmSettings: vi.fn(() => Promise.resolve()),
      getLlmUsage: vi.fn(() => Promise.resolve(makeUsage())),
      getClassifierMix: vi.fn(() => Promise.resolve({ classifier_mix: MIX })),
      onSessionExpired,
      toastSuccess,
      toastError,
    };
    api = null;

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    render("user-a");
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.unstubAllGlobals();
  });

  it("a usage-range change mid-flight does not void an in-flight credential test", async () => {
    const test = deferred<LlmTestResult>();
    deps.testLlmSettings = vi.fn(() => test.promise);

    await act(async () => {
      void api!.doTestLlmSettings();
    });
    expect(api!.llmTesting).toBe(true);

    // This bumps llmUsageGenRef (and refetches usage under its own,
    // separate generation) -- it must not touch llmCredentialGenRef.
    act(() => {
      api!.changeLlmUsageDays(7);
    });
    expect(api!.llmUsageDays).toBe(7);

    await act(async () => {
      test.resolve({ ok: true, latency_ms: 42, error: null });
      await test.promise;
    });

    // If llmCredentialGenRef and llmUsageGenRef were ever the same ref, the
    // range change above would have bumped the test's guard too, and this
    // result would be discarded -- testResult would stay null and llmTesting
    // would settle with no error and no result to show for it.
    expect(api!.llmTestResult).toEqual({ ok: true, latency_ms: 42, error: null });
    expect(api!.llmTesting).toBe(false);
  });

  it("an overlapping refreshLlmUsage call resolving last does not overwrite the newer result", async () => {
    const older = deferred<LlmUsage>();
    const newer = deferred<LlmUsage>();
    const usageCalls = [older, newer];
    let usageCallIndex = 0;
    deps.getLlmUsage = vi.fn(() => usageCalls[usageCallIndex++]!.promise);

    // Two overlapping fetches, e.g. openLlmSettings' own usage fetch
    // followed by openLlmUsage's before the first settles.
    await act(async () => {
      void api!.refreshLlmUsage(30);
    });
    await act(async () => {
      void api!.refreshLlmUsage(7);
    });

    // The newer call settles first.
    await act(async () => {
      newer.resolve(makeUsage({ window_days: 7 }));
      await newer.promise;
    });
    expect(api!.llmUsage).toEqual(makeUsage({ window_days: 7 }));

    // The older call settling last must not clobber the newer snapshot --
    // if refreshLlmUsage read llmUsageGenRef.current instead of claiming its
    // own generation, both calls would share the same generation number and
    // this stale response would win.
    await act(async () => {
      older.resolve(makeUsage({ window_days: 30 }));
      await older.promise;
    });
    expect(api!.llmUsage).toEqual(makeUsage({ window_days: 7 }));
  });

  it("an account change discards an in-flight settings fetch instead of applying it", async () => {
    const settingsFetch = deferred<LlmSettings>();
    deps.getLlmSettings = vi.fn(() => settingsFetch.promise);

    await act(async () => {
      void api!.refreshLlmSettings();
    });
    expect(api!.llmSettings).toBeNull();

    // Switching accounts bumps llmSettingsGenRef and clears state -- the
    // fetch above belongs to user-a and must not be allowed to land.
    render("user-b");
    expect(api!.llmSettings).toBeNull();

    await act(async () => {
      settingsFetch.resolve(makeSettings({ key_suffix: "user-a-suffix" }));
      await settingsFetch.promise.catch(() => {});
    });

    // Without the guard, this would now show user-a's key_suffix on
    // user-b's panel -- and it would stick, since openLlmSettings only
    // refetches `if (!llmSettings)`.
    expect(api!.llmSettings).toBeNull();
  });

  it("an account change discards an in-flight usage fetch instead of applying it", async () => {
    const usageFetch = deferred<LlmUsage>();
    deps.getLlmUsage = vi.fn(() => usageFetch.promise);

    await act(async () => {
      void api!.refreshLlmUsage(30);
    });
    expect(api!.llmUsage).toBeNull();

    render("user-b");
    expect(api!.llmUsage).toBeNull();

    await act(async () => {
      usageFetch.resolve(makeUsage({ window_days: 30 }));
      await usageFetch.promise.catch(() => {});
    });

    expect(api!.llmUsage).toBeNull();
  });

  it("an account change discards an in-flight classifier-mix fetch instead of applying it", async () => {
    const mixFetch = deferred<{ classifier_mix: ClassifierMixEntry[] }>();
    deps.getClassifierMix = vi.fn(() => mixFetch.promise);

    await act(async () => {
      void api!.refreshClassifierMix();
    });
    expect(api!.classifierMix).toBeNull();

    render("user-b");
    expect(api!.classifierMix).toBeNull();

    await act(async () => {
      mixFetch.resolve({ classifier_mix: MIX });
      await mixFetch.promise.catch(() => {});
    });

    expect(api!.classifierMix).toBeNull();
  });

  it("refreshLlmUsage rejecting with a non-401 sets llmUsageError without a toast", async () => {
    deps.getLlmUsage = vi.fn(() => Promise.reject(new Error("usage outage")));

    await act(async () => {
      await api!.refreshLlmUsage(30);
    });

    // A usage outage must stay quiet -- the panel renders its own fallback,
    // so no toast should fire for this one.
    expect(api!.llmUsageError).toBe(true);
    expect(api!.llmUsage).toBeNull();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("refreshClassifierMix rejecting with a non-401 sets classifierMixError without a toast", async () => {
    deps.getClassifierMix = vi.fn(() => Promise.reject(new Error("mix outage")));

    await act(async () => {
      await api!.refreshClassifierMix();
    });

    expect(api!.classifierMixError).toBe(true);
    expect(api!.classifierMix).toBeNull();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("refreshLlmUsage rejecting with a 401 calls onSessionExpired instead of setting an error", async () => {
    deps.getLlmUsage = vi.fn(() => Promise.reject(new ApiError(401, "expired")));

    await act(async () => {
      await api!.refreshLlmUsage(30);
    });

    expect(onSessionExpired).toHaveBeenCalledTimes(1);
    expect(api!.llmUsageError).toBe(false);
  });

  it("refreshClassifierMix rejecting with a 401 calls onSessionExpired instead of setting an error", async () => {
    deps.getClassifierMix = vi.fn(() => Promise.reject(new ApiError(401, "expired")));

    await act(async () => {
      await api!.refreshClassifierMix();
    });

    expect(onSessionExpired).toHaveBeenCalledTimes(1);
    expect(api!.classifierMixError).toBe(false);
  });

  it("a stale refreshLlmUsage rejection is discarded by the generation guard", async () => {
    const usageFetch = deferred<LlmUsage>();
    deps.getLlmUsage = vi.fn(() => usageFetch.promise);

    await act(async () => {
      void api!.refreshLlmUsage(30);
    });
    expect(api!.llmUsageError).toBe(false);

    // Bumps llmUsageGenRef -- the fetch above belongs to the previous
    // generation and its rejection landing below must not touch state,
    // same as a stale success would be discarded.
    render("user-b");

    await act(async () => {
      usageFetch.reject(new Error("stale failure"));
      await usageFetch.promise.catch(() => {});
    });

    expect(api!.llmUsageError).toBe(false);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("a stale refreshClassifierMix rejection is discarded by the generation guard", async () => {
    const mixFetch = deferred<{ classifier_mix: ClassifierMixEntry[] }>();
    deps.getClassifierMix = vi.fn(() => mixFetch.promise);

    await act(async () => {
      void api!.refreshClassifierMix();
    });
    expect(api!.classifierMixError).toBe(false);

    render("user-b");

    await act(async () => {
      mixFetch.reject(new Error("stale failure"));
      await mixFetch.promise.catch(() => {});
    });

    expect(api!.classifierMixError).toBe(false);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("a save does not discard its own write, even if a concurrent remove bumps the credential generation", async () => {
    const save = deferred<LlmSettings>();
    deps.putLlmSettings = vi.fn(() => save.promise);

    // Started outside act -- storing the raw promise, not an act() scope's
    // promise, so the remove's act below and the resolve's act further down
    // never overlap with an unflushed one still pending from this call.
    const savePromise = api!.doSaveLlmSettings({ provider: "openai", model: "gpt-4o-mini" });

    // A concurrent remove finishing bumps llmCredentialGenRef (and
    // llmUsageGenRef/llmMixGenRef) in its own finally block -- but NOT
    // llmSettingsGenRef. If doSaveLlmSettings ever guarded its own write on
    // the credential ref instead of the settings one, this bump landing
    // mid-flight would make the save below discard its own response.
    await act(async () => {
      await api!.doRemoveLlmSettings();
    });

    const newSettings = makeSettings({ provider: "gemini", model: "gemini-2.0-flash" });
    await act(async () => {
      save.resolve(newSettings);
      await savePromise;
    });

    expect(api!.llmSettings).toEqual(newSettings);
    expect(toastSuccess).toHaveBeenCalledWith("AI settings saved");
  });

  it("an account change clears everything and closes both panels", () => {
    act(() => {
      api!.setLlmSettingsOpen(true);
      api!.setLlmUsageOpen(true);
    });
    expect(api!.llmSettingsOpen).toBe(true);
    expect(api!.llmUsageOpen).toBe(true);

    render("user-b");

    expect(api!.llmSettingsOpen).toBe(false);
    expect(api!.llmUsageOpen).toBe(false);
    expect(api!.llmSettings).toBeNull();
    expect(api!.llmUsage).toBeNull();
    expect(api!.classifierMix).toBeNull();
    expect(api!.llmTestResult).toBeNull();
  });
});
