import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TopBar } from "./TopBar";
import type { Connection, User } from "@/lib/types";
import type { SyncHealth } from "@/lib/api";

// The slim accounts popover (settings-card plan §3.1) -- read-only status
// plus one settings button. No connect/disconnect/toggle affordances belong
// here anymore; that's all moved to the settings dialog.

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

const USER: User = { id: "u1", email: "alice@gmail.com", display_name: null };

function baseProps(overrides: Partial<React.ComponentProps<typeof TopBar>> = {}) {
  return {
    user: USER,
    overview: null,
    ingesting: false,
    backfilling: false,
    currentBucket: "needs_reply" as const,
    onIngest: vi.fn(),
    onBackfill: vi.fn(),
    onLogout: vi.fn(),
    ingestOpen: false,
    onIngestOpenChange: vi.fn(),
    backfillOpen: false,
    onBackfillOpenChange: vi.fn(),
    layoutOpen: false,
    onLayoutOpenChange: vi.fn(),
    arrangement: { sidebar: "left" as const, reading: "right" as const },
    onArrangement: vi.fn(),
    theme: "dark" as const,
    onTheme: vi.fn(),
    autoSync: 0,
    onAutoSync: vi.fn(),
    connections: [] as Connection[],
    health: null as SyncHealth | null,
    accountsOpen: true,
    onAccountsOpenChange: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenAiSettings: vi.fn(),
    llmUsable: null,
    ...overrides,
  };
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

function render(props: ReturnType<typeof baseProps>) {
  act(() => {
    root.render(<TopBar {...props} />);
  });
}

function findButton(text: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === text,
  );
}

describe("TopBar accounts popover", () => {
  it("shows a read-only status line per account and no management controls", () => {
    render(
      baseProps({
        connections: [
          makeConnection({
            id: "conn-1",
            email_address: "alice@gmail.com",
            label_sync_enabled: true,
            label_sync_drift: 12,
          }),
          makeConnection({
            id: "conn-2",
            email_address: "bob@outlook.com",
            provider: "outlook",
            label_sync_enabled: false,
          }),
        ],
      }),
    );

    expect(container.textContent).toContain("alice@gmail.com");
    expect(container.textContent).toContain("labels syncing — 12 remaining");
    expect(container.textContent).toContain("bob@outlook.com");
    expect(container.textContent).toContain("labels off");

    // No management affordances left in the popover -- those all moved to
    // the settings dialog.
    expect(findButton("disconnect")).toBeUndefined();
    expect(findButton("confirm")).toBeUndefined();
    expect(findButton("connect another gmail")).toBeUndefined();
    expect(findButton("connect outlook")).toBeUndefined();
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("reauth takes precedence over an enabled toggle's drift count", () => {
    render(
      baseProps({
        connections: [
          makeConnection({
            label_sync_enabled: true,
            label_sync_drift: 5,
            reauth_required: true,
          }),
        ],
      }),
    );

    expect(container.textContent).toContain("labels paused — reconnect");
    expect(container.textContent).not.toContain("labels syncing");
  });

  it("reads 'labels synced' once enabled with no drift left", () => {
    render(
      baseProps({
        connections: [
          makeConnection({ label_sync_enabled: true, label_sync_drift: 0 }),
        ],
      }),
    );

    expect(container.textContent).toContain("labels synced");
  });

  it("shows the empty state with no connections", () => {
    render(baseProps({ connections: [] }));
    expect(container.textContent).toContain("no accounts connected");
  });

  it("the settings button closes the popover and opens the settings dialog", () => {
    const onOpenSettings = vi.fn();
    const onAccountsOpenChange = vi.fn();
    render(baseProps({ onOpenSettings, onAccountsOpenChange }));

    const settingsBtn = findButton("settings");
    expect(settingsBtn).toBeDefined();
    expect(settingsBtn?.disabled).toBe(false);

    act(() => {
      settingsBtn!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onAccountsOpenChange).toHaveBeenCalledWith(false);
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it("disables the settings button while the tour has this popover locked open", () => {
    render(baseProps({ accountsLocked: true }));
    const settingsBtn = findButton("settings");
    expect(settingsBtn?.disabled).toBe(true);
  });
});
