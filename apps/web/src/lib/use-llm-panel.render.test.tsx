import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError } from "@/lib/api";
import type { SettingsTab } from "@/components/console/SettingsDialog";
import type {
  ClassifierMixEntry,
  LlmCredentialSummary,
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
    classification_fallback_local: false,
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

function makeCredential(overrides: Partial<LlmCredentialSummary> = {}): LlmCredentialSummary {
  return {
    id: "cred-1",
    name: "default",
    provider: "openai",
    model: "gpt-4o-mini",
    key_suffix: "abcd",
    last_verified_at: null,
    active: true,
    classification_byok: false,
    classification_fallback_local: false,
    ...overrides,
  };
}

describe("useLlmPanel rendered lifecycle", () => {
  let root: Root;
  let container: HTMLElement;
  let deps: LlmPanelDeps;
  let api: ReturnType<typeof useLlmPanel> | null;
  let onSessionExpired: Mock<() => void>;
  let toastSuccess: Mock<(message: string) => void>;
  let toastError: Mock<(message: string) => void>;
  // Stands in for App's tab-activation handler -- this hook only ever needs
  // to say WHICH tab an entry point wants open (settings-card plan §3.3).
  let openSettings: Mock<(tab: SettingsTab) => void>;

  function Harness({ userId }: { userId: string | null }) {
    api = useLlmPanel({ userId, deps, openSettings });
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
    openSettings = vi.fn<(tab: SettingsTab) => void>();
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
          classification_fallback_local?: boolean;
        }) => Promise.resolve(makeSettings({ provider: input.provider, model: input.model })),
      ),
      testLlmSettings: vi.fn(() =>
        Promise.resolve<LlmTestResult>({ ok: true, latency_ms: 12, error: null }),
      ),
      deleteLlmSettings: vi.fn(() => Promise.resolve()),
      getLlmUsage: vi.fn(() => Promise.resolve(makeUsage())),
      getClassifierMix: vi.fn(() => Promise.resolve({ classifier_mix: MIX })),
      listLlmCredentials: vi.fn(() => Promise.resolve([makeCredential()])),
      createLlmCredential: vi.fn((input: { name: string }) =>
        Promise.resolve(makeCredential({ id: "cred-new", name: input.name })),
      ),
      activateLlmCredential: vi.fn((id: string) =>
        Promise.resolve(makeCredential({ id, active: true })),
      ),
      deleteLlmCredential: vi.fn(() => Promise.resolve()),
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

  it("an account change clears everything, including a stuck settings error", async () => {
    // The settings dialog itself now closes via App's own account-scoped
    // reset (settingsPanel -> null, settings-card plan §3.4) -- this hook
    // only owns the DATA a user change has to wipe, which is what's left to
    // check here.
    deps.getLlmSettings = vi.fn(() => Promise.reject(new Error("outage")));
    await act(async () => {
      await api!.refreshLlmSettings();
    });
    expect(api!.llmSettingsError).toBe(true);

    render("user-b");

    expect(api!.llmSettingsError).toBe(false);
    expect(api!.llmSettings).toBeNull();
    expect(api!.llmUsage).toBeNull();
    expect(api!.classifierMix).toBeNull();
    expect(api!.llmTestResult).toBeNull();
  });

  it("openLlmSettings clears a stale test result and opens the ai tab", async () => {
    await act(async () => {
      await api!.doTestLlmSettings();
    });
    expect(api!.llmTestResult).not.toBeNull();

    act(() => {
      api!.openLlmSettings();
    });

    expect(api!.llmTestResult).toBeNull();
    expect(openSettings).toHaveBeenCalledWith("ai");
  });

  it("openLlmUsage opens the usage tab", () => {
    act(() => {
      api!.openLlmUsage();
    });
    expect(openSettings).toHaveBeenCalledWith("usage");
  });

  describe("llmSettingsError", () => {
    it("is set on a load failure and does not toast (silent login-time refresh)", async () => {
      deps.getLlmSettings = vi.fn(() => Promise.reject(new Error("outage")));

      await act(async () => {
        await api!.refreshLlmSettings();
      });

      expect(api!.llmSettingsError).toBe(true);
      expect(toastError).not.toHaveBeenCalled();
    });

    it("retryLlmSettings toasts on failure -- it's user-initiated", async () => {
      deps.getLlmSettings = vi.fn(() => Promise.reject(new Error("still down")));

      await act(async () => {
        await api!.retryLlmSettings();
      });

      expect(api!.llmSettingsError).toBe(true);
      expect(toastError).toHaveBeenCalledWith("still down");
    });

    it("is cleared by a subsequent successful retry", async () => {
      deps.getLlmSettings = vi.fn(() => Promise.reject(new Error("outage")));
      await act(async () => {
        await api!.refreshLlmSettings();
      });
      expect(api!.llmSettingsError).toBe(true);

      deps.getLlmSettings = vi.fn(() => Promise.resolve(makeSettings()));
      await act(async () => {
        await api!.retryLlmSettings();
      });

      expect(api!.llmSettingsError).toBe(false);
      expect(api!.llmSettings).toEqual(makeSettings());
    });

    it("a stale (superseded-generation) failure is discarded, not applied", async () => {
      const settingsFetch = deferred<LlmSettings>();
      deps.getLlmSettings = vi.fn(() => settingsFetch.promise);

      await act(async () => {
        void api!.refreshLlmSettings();
      });
      expect(api!.llmSettingsError).toBe(false);

      // Switching accounts bumps llmSettingsGenRef -- the fetch above
      // belongs to the previous generation.
      render("user-b");

      await act(async () => {
        settingsFetch.reject(new Error("stale failure"));
        await settingsFetch.promise.catch(() => {});
      });

      expect(api!.llmSettingsError).toBe(false);
      expect(toastError).not.toHaveBeenCalled();
    });
  });

  describe("multi-credential list", () => {
    it("refreshLlmCredentials loads the list", async () => {
      const items = [makeCredential({ id: "cred-1" }), makeCredential({ id: "cred-2" })];
      deps.listLlmCredentials = vi.fn(() => Promise.resolve(items));

      await act(async () => {
        await api!.refreshLlmCredentials();
      });

      expect(api!.llmCredentials).toEqual(items);
    });

    it("doCreateLlmCredential adds the new row and refreshes settings -- a first credential is created active", async () => {
      deps.listLlmCredentials = vi.fn(() =>
        Promise.resolve([makeCredential({ id: "cred-new", name: "Work key" })]),
      );
      deps.getLlmSettings = vi.fn(() =>
        Promise.resolve(makeSettings({ provider: "gemini", model: "gemini-2.0-flash" })),
      );

      await act(async () => {
        await api!.doCreateLlmCredential({
          name: "Work key",
          provider: "gemini",
          api_key: "gm-key-12345678",
          model: "gemini-2.0-flash",
        });
      });

      expect(api!.llmCredentials).toEqual([makeCredential({ id: "cred-new", name: "Work key" })]);
      expect(api!.llmSettings).toEqual(
        makeSettings({ provider: "gemini", model: "gemini-2.0-flash" }),
      );
      expect(toastSuccess).toHaveBeenCalledWith('Added "Work key"');
      expect(api!.llmCreatingCredential).toBe(false);
    });

    it("a create failure toasts and leaves the list untouched", async () => {
      deps.createLlmCredential = vi.fn(() =>
        Promise.reject(new ApiError(422, "a credential with this name already exists")),
      );

      await act(async () => {
        await api!.doCreateLlmCredential({
          name: "dup",
          provider: "openai",
          api_key: "sk-12345678",
          model: "gpt-4o-mini",
        });
      });

      expect(toastError).toHaveBeenCalledWith("a credential with this name already exists");
      expect(api!.llmCredentials).toBeNull();
    });

    it("a create failure at the cap surfaces the server's 422 message", async () => {
      deps.createLlmCredential = vi.fn(() =>
        Promise.reject(new ApiError(422, "credential limit reached")),
      );

      await act(async () => {
        await api!.doCreateLlmCredential({
          name: "sixth",
          provider: "openai",
          api_key: "sk-12345678",
          model: "gpt-4o-mini",
        });
      });

      expect(toastError).toHaveBeenCalledWith("credential limit reached");
    });

    it("doActivateLlmCredential switches the active credential and refreshes both list and settings", async () => {
      deps.listLlmCredentials = vi.fn(() =>
        Promise.resolve([
          makeCredential({ id: "cred-1", active: false }),
          makeCredential({ id: "cred-2", active: true }),
        ]),
      );
      deps.getLlmSettings = vi.fn(() =>
        Promise.resolve(makeSettings({ key_suffix: "cred-2-suffix" })),
      );

      await act(async () => {
        await api!.doActivateLlmCredential("cred-2");
      });

      expect(api!.llmCredentials).toEqual([
        makeCredential({ id: "cred-1", active: false }),
        makeCredential({ id: "cred-2", active: true }),
      ]);
      expect(api!.llmSettings).toEqual(makeSettings({ key_suffix: "cred-2-suffix" }));
      expect(api!.llmActivatingCredentialId).toBeNull();
    });

    it("doDeleteLlmCredential removes an inactive row and refreshes the list", async () => {
      deps.deleteLlmCredential = vi.fn(() => Promise.resolve());
      deps.listLlmCredentials = vi.fn(() => Promise.resolve([makeCredential({ id: "cred-1" })]));

      await act(async () => {
        await api!.doDeleteLlmCredential("cred-2");
      });

      expect(deps.deleteLlmCredential).toHaveBeenCalledWith("cred-2");
      expect(api!.llmCredentials).toEqual([makeCredential({ id: "cred-1" })]);
      expect(toastSuccess).toHaveBeenCalledWith("Credential removed");
    });

    it("a 409 deleting the active credential sets an inline row error, not a toast", async () => {
      deps.deleteLlmCredential = vi.fn(() =>
        Promise.reject(new ApiError(409, "cannot delete the active credential")),
      );

      await act(async () => {
        await api!.doDeleteLlmCredential("cred-2");
      });

      expect(api!.llmCredentialDeleteError).toEqual({
        id: "cred-2",
        message: "This is your active credential — switch to another one first.",
      });
      expect(toastError).not.toHaveBeenCalled();
    });

    it("a non-409 delete failure toasts instead of setting the row error", async () => {
      deps.deleteLlmCredential = vi.fn(() => Promise.reject(new Error("network down")));

      await act(async () => {
        await api!.doDeleteLlmCredential("cred-2");
      });

      expect(toastError).toHaveBeenCalledWith("network down");
      expect(api!.llmCredentialDeleteError).toBeNull();
    });

    it("an account change clears the credential list and any pending row error", async () => {
      deps.deleteLlmCredential = vi.fn(() =>
        Promise.reject(new ApiError(409, "cannot delete the active credential")),
      );
      await act(async () => {
        await api!.doDeleteLlmCredential("cred-2");
      });
      expect(api!.llmCredentialDeleteError).not.toBeNull();

      await act(async () => {
        await api!.refreshLlmCredentials();
      });
      expect(api!.llmCredentials).not.toBeNull();

      render("user-b");

      expect(api!.llmCredentials).toBeNull();
      expect(api!.llmCredentialDeleteError).toBeNull();
    });

    it("the singular kill switch clears the credential list too, not just llmSettings", async () => {
      await act(async () => {
        await api!.refreshLlmCredentials();
      });
      expect(api!.llmCredentials).not.toBeNull();

      await act(async () => {
        await api!.doRemoveLlmSettings();
      });

      expect(api!.llmCredentials).toEqual([]);
    });
  });
});
