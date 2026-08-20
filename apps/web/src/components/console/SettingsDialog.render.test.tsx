import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsDialog, type SettingsTab } from "./SettingsDialog";
import type {
  ClassifierMixEntry,
  Connection,
  LlmCredentialSummary,
  LlmProvider,
  LlmSettings,
  LlmUsage,
} from "@/lib/types";
import type { SyncHealth } from "@/lib/api";

function makeConnection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: "conn-1",
    provider: "gmail",
    email_address: "alice@gmail.com",
    created_at: "2026-08-01T00:00:00Z",
    reauth_required: false,
    label_sync_enabled: false,
    label_sync_drift: null,
    ...overrides,
  };
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

// Content stays in the DOM while a tab is inactive (hidden via the `hidden`
// attribute) -- this walks up from a found element to check whether any
// ancestor (including itself) carries that attribute, the same signal a
// screen reader / browser layout would use.
function isHiddenByAncestry(el: Element): boolean {
  let cur: Element | null = el;
  while (cur) {
    if (cur instanceof HTMLElement && cur.hidden) return true;
    cur = cur.parentElement;
  }
  return false;
}

function findByText(text: string): HTMLElement {
  const all = Array.from(document.body.querySelectorAll<HTMLElement>("button, p, span, div"));
  const el = all.find((e) => e.textContent?.trim() === text && e.children.length === 0);
  if (!el) throw new Error(`element with text "${text}" not found`);
  return el;
}

function findButton(text: string): HTMLButtonElement {
  const btn = Array.from(document.body.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === text,
  );
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function findCheckboxByLabelText(text: string): HTMLInputElement {
  const label = Array.from(document.body.querySelectorAll("label")).find((l) =>
    l.textContent?.includes(text),
  );
  if (!label) throw new Error(`checkbox label containing "${text}" not found`);
  const input = label.querySelector('input[type="checkbox"]');
  if (!input) throw new Error(`no checkbox inside label containing "${text}"`);
  return input as HTMLInputElement;
}

function findRadioByLabelText(text: string): HTMLInputElement {
  const label = Array.from(document.body.querySelectorAll("label")).find((l) =>
    l.textContent?.includes(text),
  );
  if (!label) throw new Error(`radio label containing "${text}" not found`);
  const input = label.querySelector('input[type="radio"]');
  if (!input) throw new Error(`no radio inside label containing "${text}"`);
  return input as HTMLInputElement;
}

// React patches the input element's own `value` setter to track what it
// last rendered -- writing through it directly (`el.value = ...`) leaves
// that tracker in sync, so a follow-up "input" event looks like a no-op and
// never reaches onChange. Calling the native prototype's setter instead
// bypasses the patch, same trick ReplyComposer's own render test uses.
function setInputValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!
    .set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

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

function Harness({
  initialTab = "accounts",
  connections = [],
  health = null,
  settings = null,
  settingsError = false,
  usage = null,
  usageError = false,
  onDisconnect = vi.fn(),
  onConnectionUpdated = vi.fn(),
  onRetrySettings = vi.fn(),
  onSave = vi.fn(),
  heuristicNoticeDismissed = false,
  onDismissHeuristicNotice = vi.fn(),
  classifierMix = null,
  credentials = null,
  onCreateCredential = vi.fn(),
  creatingCredential = false,
  onActivateCredential = vi.fn(),
  activatingCredentialId = null,
  onDeleteCredential = vi.fn(),
  deletingCredentialId = null,
  credentialDeleteError = null,
  credentialsError = false,
}: {
  initialTab?: SettingsTab;
  connections?: Connection[];
  health?: SyncHealth | null;
  settings?: LlmSettings | null;
  settingsError?: boolean;
  usage?: LlmUsage | null;
  usageError?: boolean;
  onDisconnect?: (id: string) => void;
  onConnectionUpdated?: (c: Connection) => void;
  onRetrySettings?: () => void;
  onSave?: (input: {
    provider: LlmProvider;
    api_key?: string;
    model: string;
    base_url?: string;
    classification_byok?: boolean;
    classification_fallback_local?: boolean;
  }) => void;
  heuristicNoticeDismissed?: boolean;
  onDismissHeuristicNotice?: () => void;
  classifierMix?: ClassifierMixEntry[] | null;
  credentials?: LlmCredentialSummary[] | null;
  onCreateCredential?: (input: {
    name: string;
    provider: LlmProvider;
    api_key: string;
    model: string;
    base_url?: string;
    classification_byok?: boolean;
    classification_fallback_local?: boolean;
  }) => void;
  creatingCredential?: boolean;
  onActivateCredential?: (id: string) => void;
  activatingCredentialId?: string | null;
  onDeleteCredential?: (id: string) => void;
  deletingCredentialId?: string | null;
  credentialDeleteError?: { id: string; message: string } | null;
  credentialsError?: boolean;
}) {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  return (
    <SettingsDialog
      open={open}
      onOpenChange={setOpen}
      tab={tab}
      onTabChange={setTab}
      connections={connections}
      health={health}
      onConnectGmail={vi.fn()}
      onConnectOutlook={undefined}
      onDisconnect={onDisconnect}
      onConnectionUpdated={onConnectionUpdated}
      settings={settings}
      settingsError={settingsError}
      onRetrySettings={onRetrySettings}
      onSave={onSave}
      saving={false}
      onTest={vi.fn()}
      testing={false}
      testResult={null}
      onRemove={vi.fn()}
      removing={false}
      classifierMix={classifierMix}
      classifierMixError={false}
      heuristicNoticeDismissed={heuristicNoticeDismissed}
      onDismissHeuristicNotice={onDismissHeuristicNotice}
      credentials={credentials}
      onCreateCredential={onCreateCredential}
      creatingCredential={creatingCredential}
      onActivateCredential={onActivateCredential}
      activatingCredentialId={activatingCredentialId}
      onDeleteCredential={onDeleteCredential}
      deletingCredentialId={deletingCredentialId}
      credentialDeleteError={credentialDeleteError}
      credentialsError={credentialsError}
      usage={usage}
      usageError={usageError}
      days={30}
      onDaysChange={vi.fn()}
    />
  );
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
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

describe("SettingsDialog tab mounting", () => {
  it("keeps all three sections mounted across a tab switch, only toggling `hidden`", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="accounts"
          connections={[makeConnection()]}
          settings={makeSettings()}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });

    const accountsMarker = () => findButton("connect another gmail");
    const aiMarker = () => findByText("save your own API key and CortexMail uses it to pull deadlines and to-dos out of your mail.");
    const usageMarker = () => document.body.querySelector('[role="group"][aria-label="usage window"]')!;

    expect(isHiddenByAncestry(accountsMarker())).toBe(false);
    expect(isHiddenByAncestry(aiMarker())).toBe(true);
    expect(isHiddenByAncestry(usageMarker())).toBe(true);

    act(() => {
      findButton("ai model").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    // All three still exist in the DOM -- switching tabs never unmounts them.
    expect(isHiddenByAncestry(accountsMarker())).toBe(true);
    expect(isHiddenByAncestry(aiMarker())).toBe(false);
    expect(isHiddenByAncestry(usageMarker())).toBe(true);
  });
});

describe("SettingsDialog accounts tab", () => {
  it("renders a card per connection with a two-step disconnect", async () => {
    const onDisconnect = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="accounts"
          connections={[makeConnection({ email_address: "alice@gmail.com" })]}
          onDisconnect={onDisconnect}
        />,
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("alice@gmail.com");

    const btn = findButton("disconnect");
    act(() => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    // First click arms it -- no disconnect fired yet, button now reads confirm.
    expect(onDisconnect).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("confirm");

    const confirmBtn = findButton("confirm");
    act(() => {
      confirmBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onDisconnect).toHaveBeenCalledWith("conn-1");
  });
});

describe("SettingsDialog ai tab", () => {
  it("shows loading… while settings is null and not errored", async () => {
    await act(async () => {
      root.render(<Harness initialTab="ai" settings={null} settingsError={false} />);
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("loading…");
  });

  it("shows an error state with a retry button when settingsError is true", async () => {
    const onRetrySettings = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={null}
          settingsError={true}
          onRetrySettings={onRetrySettings}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).not.toContain("loading…");
    const retryBtn = findButton("retry");
    act(() => {
      retryBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onRetrySettings).toHaveBeenCalledTimes(1);
  });

  it("view usage switches to the usage tab", async () => {
    await act(async () => {
      root.render(<Harness initialTab="ai" settings={makeSettings()} usage={makeUsage()} />);
      await Promise.resolve();
    });

    const viewUsageBtn = findButton("view usage");
    act(() => {
      viewUsageBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    const usageTabBtn = findButton("usage");
    expect(usageTabBtn.getAttribute("aria-pressed")).toBe("true");
    const usageMarker = document.body.querySelector('[role="group"][aria-label="usage window"]')!;
    expect(isHiddenByAncestry(usageMarker)).toBe(false);
  });
});

describe("SettingsDialog ai tab LLM-failure fallback toggle", () => {
  it("does not render the fallback toggle when classification_byok is off", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classification_byok: false })}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).not.toContain(
      "If your LLM fails, classify with the built-in model instead",
    );
  });

  it("renders the fallback toggle, and its two-sentence copy, once classification_byok is on", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({
            classification_byok: true,
            classification_fallback_local: false,
          })}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain(
      "If your LLM fails, classify with the built-in model instead",
    );
    expect(document.body.textContent).toContain(
      "Off means affected mail stays unclassified until you run a backfill.",
    );
    expect(findCheckboxByLabelText("If your LLM fails").checked).toBe(false);
  });

  it("saves the toggled value through onSave", async () => {
    const onSave = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({
            classification_byok: true,
            classification_fallback_local: false,
          })}
          usage={makeUsage()}
          onSave={onSave}
        />,
      );
      await Promise.resolve();
    });

    const toggle = findCheckboxByLabelText("If your LLM fails");
    act(() => {
      toggle.click();
    });
    expect(toggle.checked).toBe(true);

    act(() => {
      findButton("save").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toMatchObject({ classification_fallback_local: true });
  });

  it("hides the toggle again if classification_byok gets unchecked before saving", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({
            classification_byok: true,
            classification_fallback_local: true,
          })}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });

    const byokToggle = findCheckboxByLabelText("Also use my key to sort incoming mail");
    act(() => {
      byokToggle.click();
    });
    expect(document.body.textContent).not.toContain(
      "If your LLM fails, classify with the built-in model instead",
    );
  });
});

describe("SettingsDialog ai tab heuristic-backend notice", () => {
  it("renders when the backend is heuristic and not dismissed", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classifier_backend: "heuristic" })}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain(
      "Keyword rules are sorting your mail.",
    );
  });

  it("does not render when the backend isn't heuristic", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classifier_backend: "auto" })}
          usage={makeUsage()}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).not.toContain(
      "Keyword rules are sorting your mail.",
    );
  });

  it("hides once dismissed", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classifier_backend: "heuristic" })}
          usage={makeUsage()}
          heuristicNoticeDismissed={true}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).not.toContain(
      "Keyword rules are sorting your mail.",
    );
  });

  it("fires the dismiss callback", async () => {
    const onDismissHeuristicNotice = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classifier_backend: "heuristic" })}
          usage={makeUsage()}
          onDismissHeuristicNotice={onDismissHeuristicNotice}
        />,
      );
      await Promise.resolve();
    });
    act(() => {
      findButton("dismiss").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onDismissHeuristicNotice).toHaveBeenCalledTimes(1);
  });
});

describe("SettingsDialog ai tab multi-credential list", () => {
  it("shows an inline error when the credential list fetch failed", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentialsError={true}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("could not load your credential list");
  });

  it("does not render the credential list for a single credential -- no clutter for the common case", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[makeCredential()]}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).not.toContain("your credentials");
  });

  it("renders the list with the active credential marked once there are 2+", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("your credentials");
    expect(findRadioByLabelText("Work").checked).toBe(true);
    expect(findRadioByLabelText("Personal").checked).toBe(false);
  });

  it("hides the remove button for the active row but shows it for inactive ones", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
        />,
      );
      await Promise.resolve();
    });
    // Only one "remove this credential" button in the LIST -- deleting the
    // active credential has to go through the singular kill switch instead
    // (its own "remove" button, further down the form, is a different
    // control -- title-scoped here so this doesn't count that one too).
    const removeButtons = Array.from(document.body.querySelectorAll("button")).filter(
      (b) => b.title === "remove this credential",
    );
    expect(removeButtons.length).toBe(1);
  });

  it("clicking an inactive credential's radio fires onActivateCredential with its id", async () => {
    const onActivateCredential = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
          onActivateCredential={onActivateCredential}
        />,
      );
      await Promise.resolve();
    });
    act(() => {
      findRadioByLabelText("Personal").click();
    });
    expect(onActivateCredential).toHaveBeenCalledWith("cred-b");
  });

  it("clicking remove on an inactive credential fires onDeleteCredential with its id", async () => {
    const onDeleteCredential = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
          onDeleteCredential={onDeleteCredential}
        />,
      );
      await Promise.resolve();
    });
    act(() => {
      findButton("remove").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onDeleteCredential).toHaveBeenCalledWith("cred-b");
  });

  it("shows a 409 delete error inline on the row it's about, not as a toast", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
          credentialDeleteError={{
            id: "cred-b",
            message: "This is your active credential — switch to another one first.",
          }}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain(
      "This is your active credential — switch to another one first.",
    );
  });

  it("add credential switches the form into create mode and submits through onCreateCredential", async () => {
    const onCreateCredential = vi.fn();
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          onCreateCredential={onCreateCredential}
        />,
      );
      await Promise.resolve();
    });

    act(() => {
      findButton("+ add another credential").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    // The form now needs a name, and the API key field no longer offers to
    // keep the saved one -- there's nothing stored yet for this new row.
    expect(document.body.textContent).toContain(
      "your key is encrypted, stored, and never shown again after you save it",
    );

    const nameInput = document.body.querySelector('input[maxlength="60"]') as HTMLInputElement;
    act(() => {
      setInputValue(nameInput, "Personal key");
    });
    const keyInput = document.body.querySelector('input[type="password"]') as HTMLInputElement;
    act(() => {
      setInputValue(keyInput, "sk-personal-key-12345678");
    });
    const modelInput = document.body.querySelector(
      'input[placeholder="e.g. gpt-4o-mini"]',
    ) as HTMLInputElement;
    act(() => {
      setInputValue(modelInput, "gpt-4o-mini");
    });

    act(() => {
      findButton("add credential").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onCreateCredential).toHaveBeenCalledTimes(1);
    expect(onCreateCredential.mock.calls[0][0]).toMatchObject({
      name: "Personal key",
      provider: "openai",
      model: "gpt-4o-mini",
      api_key: "sk-personal-key-12345678",
    });
  });

  it("disables the other row's delete button and add-credential submit while an activate is pending", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings()}
          usage={makeUsage()}
          credentials={[
            makeCredential({ id: "cred-a", name: "Work", active: true }),
            makeCredential({ id: "cred-b", name: "Personal", active: false }),
          ]}
          activatingCredentialId="cred-b"
        />,
      );
      await Promise.resolve();
    });

    // Activate and delete aren't optimistic-locked against each other
    // server-side -- if delete stayed clickable here, it could win the
    // race and the pending activate would 404 a moment later.
    const removeButtons = Array.from(document.body.querySelectorAll("button")).filter(
      (b) => b.title === "remove this credential",
    );
    expect(removeButtons.length).toBe(1);
    expect(removeButtons[0]!.disabled).toBe(true);

    act(() => {
      findButton("+ add another credential").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(findButton("add credential").disabled).toBe(true);
  });
});

describe("SettingsDialog ai tab classifier-mix summary forward compatibility", () => {
  it("omits an entry whose kind the label map doesn't recognize, instead of rendering it broken", async () => {
    // Casts past the union on purpose -- simulating an already-open tab
    // running an old bundle that receives a `kind` its build has never heard
    // of (classifier-default-honesty plan, wave plan section).
    const mix: ClassifierMixEntry[] = [
      { kind: "local", count: 3 },
      { kind: "a-future-kind" as ClassifierMixEntry["kind"], count: 12 },
    ];
    await act(async () => {
      root.render(
        <Harness
          initialTab="ai"
          settings={makeSettings({ classifier_backend: "auto" })}
          usage={makeUsage()}
          classifierMix={mix}
        />,
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Sorting your mail: built-in model 3");
    expect(document.body.textContent).not.toContain("undefined");
    expect(document.body.textContent).not.toContain("12");
  });
});

describe("SettingsDialog ai tab provider picker", () => {
  it("offers Anthropic alongside the other presets", async () => {
    await act(async () => {
      root.render(<Harness initialTab="ai" settings={makeSettings()} usage={makeUsage()} />);
      await Promise.resolve();
    });
    const select = document.body.querySelector("select")!;
    const optionLabels = Array.from(select.querySelectorAll("option")).map((o) =>
      o.textContent?.trim(),
    );
    expect(optionLabels).toContain("Anthropic");
  });
});

describe("SettingsDialog usage tab dashboard links", () => {
  it("links Anthropic's usage dashboard when it shows up in the by-provider breakdown", async () => {
    await act(async () => {
      root.render(
        <Harness
          initialTab="usage"
          settings={makeSettings({ provider: "anthropic" })}
          usage={makeUsage({
            totals: {
              calls: 4,
              calls_with_total_tokens: 4,
              prompt_tokens: 400,
              completion_tokens: 100,
              total_tokens: 500,
            },
            by_provider: [
              {
                provider: "anthropic",
                calls: 4,
                calls_with_total_tokens: 4,
                prompt_tokens: 400,
                completion_tokens: 100,
                total_tokens: 500,
              },
            ],
          })}
        />,
      );
      await Promise.resolve();
    });
    const link = Array.from(document.body.querySelectorAll("a")).find((a) =>
      a.textContent?.includes("Anthropic"),
    );
    expect(link?.getAttribute("href")).toBe("https://console.anthropic.com/settings/usage");
  });
});

describe("SettingsDialog focus on open", () => {
  it("focuses the active tab button, not the first tabbable element", async () => {
    await act(async () => {
      root.render(<Harness initialTab="usage" settings={makeSettings()} usage={makeUsage()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.activeElement).toBe(findButton("usage"));
  });
});
