import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginScreen } from "./LoginScreen";

// Mounting LoginScreen fires listAuthOptions on mount -- stub the whole
// module so that resolves instantly and none of the real auth calls fire.
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, msg: string) {
      super(msg);
      this.status = status;
    }
  },
  USE_MOCK: true,
  listAuthOptions: vi.fn(() => Promise.resolve({ providers: [], demoLogin: false })),
  login: vi.fn(),
  signup: vi.fn(),
  forgotPassword: vi.fn(),
  demoLogin: vi.fn(),
  resendVerification: vi.fn(),
  googleAuthStart: vi.fn(),
  microsoftAuthStart: vi.fn(),
  setToken: vi.fn(),
}));

let container: HTMLDivElement;
let root: Root;

async function renderLoginScreen(onBack?: () => void) {
  await act(async () => {
    root.render(<LoginScreen onAuthed={vi.fn()} onBack={onBack} />);
    // Flushes the listAuthOptions microtask so its state update lands
    // inside act, not after -- the back control itself doesn't depend on
    // this, but an unflushed effect here would log an act warning.
    await Promise.resolve();
  });
}

function findBackButton(): HTMLButtonElement | null {
  return (
    (Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent === "← Back to CortexMail",
    ) as HTMLButtonElement | undefined) ?? null
  );
}

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

describe("LoginScreen back control", () => {
  it("fires onBack when the back control is clicked in login mode", async () => {
    const onBack = vi.fn();
    await renderLoginScreen(onBack);

    const backButton = findBackButton();
    expect(backButton).not.toBeNull();

    act(() => {
      backButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("renders no back control when onBack is omitted", async () => {
    await renderLoginScreen(undefined);

    expect(findBackButton()).toBeNull();
  });
});
