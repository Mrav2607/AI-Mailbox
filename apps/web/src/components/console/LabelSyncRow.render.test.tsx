import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Same shape as the other render tests' mock: a real ApiError class so
// `instanceof` checks inside the row work, plus a stubbed updateConnection
// each test configures.
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    errorId?: string;
    code?: string;
    constructor(status: number, msg: string, errorId?: string, code?: string) {
      super(msg);
      this.status = status;
      this.errorId = errorId;
      this.code = code;
    }
  },
  updateConnection: vi.fn(),
}));

import { ApiError, updateConnection } from "@/lib/api";
import { LabelSyncRow } from "./LabelSyncRow";
import type { Connection } from "@/lib/types";

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

function findButton(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(text),
  );
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  vi.mocked(updateConnection).mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("LabelSyncRow", () => {
  it("calls onUpdated with the PATCH response on a successful toggle", async () => {
    const updated = makeConnection({ label_sync_enabled: true, label_sync_drift: 12 });
    vi.mocked(updateConnection).mockResolvedValue(updated);
    const onUpdated = vi.fn();

    await act(async () => {
      root.render(
        <LabelSyncRow
          connection={makeConnection()}
          onConnectGmail={vi.fn()}
          onUpdated={onUpdated}
        />,
      );
    });

    const checkbox = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    await act(async () => {
      checkbox.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(updateConnection).toHaveBeenCalledWith("conn-1", { label_sync_enabled: true });
    expect(onUpdated).toHaveBeenCalledWith(updated);
  });

  it("renders the connection prop's enabled/drift state directly, with no local override", async () => {
    const onUpdated = vi.fn();
    await act(async () => {
      root.render(
        <LabelSyncRow
          connection={makeConnection({ label_sync_enabled: true, label_sync_drift: 7 })}
          onConnectGmail={vi.fn()}
          onUpdated={onUpdated}
        />,
      );
    });

    const checkbox = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    expect(container.textContent).toContain("syncing — 7 remaining");
  });

  it("shows the frozen reconnect-code error copy plus a reconnect button", async () => {
    vi.mocked(updateConnection).mockRejectedValue(
      new ApiError(409, "needs reconnect", undefined, "reauth_required"),
    );
    const onConnectGmail = vi.fn();

    await act(async () => {
      root.render(
        <LabelSyncRow
          connection={makeConnection()}
          onConnectGmail={onConnectGmail}
          onUpdated={vi.fn()}
        />,
      );
    });

    const checkbox = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    await act(async () => {
      checkbox.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain(
      "This account needs to be reconnected before label sync can turn on.",
    );
    const reconnectBtn = findButton(container, "reconnect");
    act(() => {
      reconnectBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onConnectGmail).toHaveBeenCalledTimes(1);
  });

  it("shows the frozen label_sync_busy copy with no reconnect button", async () => {
    vi.mocked(updateConnection).mockRejectedValue(
      new ApiError(409, "busy", undefined, "label_sync_busy"),
    );

    await act(async () => {
      root.render(
        <LabelSyncRow connection={makeConnection()} onConnectGmail={vi.fn()} onUpdated={vi.fn()} />,
      );
    });

    const checkbox = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    await act(async () => {
      checkbox.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("a sync is finishing — try again in a few minutes");
    expect(() => findButton(container, "reconnect")).toThrow();
  });
});
