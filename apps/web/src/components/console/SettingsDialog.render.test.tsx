import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsDialog, type SettingsTab } from "./SettingsDialog";
import type { Connection, LlmSettings, LlmUsage } from "@/lib/types";
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
      onSave={vi.fn()}
      saving={false}
      onTest={vi.fn()}
      testing={false}
      testResult={null}
      onRemove={vi.fn()}
      removing={false}
      classifierMix={null}
      classifierMixError={false}
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
