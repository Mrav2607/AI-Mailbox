import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Same shape as login-screen.render.test.tsx's mock: a real ApiError class so
// `instanceof` checks inside the component work, plus stubbed sendReply/
// draftReply the individual tests configure per-case.
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    errorId?: string;
    code?: string;
    detail?: Record<string, unknown>;
    constructor(
      status: number,
      msg: string,
      errorId?: string,
      code?: string,
      detail?: Record<string, unknown>,
    ) {
      super(msg);
      this.status = status;
      this.errorId = errorId;
      this.code = code;
      this.detail = detail;
    }
  },
  sendReply: vi.fn(),
  draftReply: vi.fn(),
}));

import { ApiError, draftReply, sendReply } from "@/lib/api";
import { ReplyComposer, type ReplyStateRefetch } from "./ReplyComposer";
import type { ReplySent, ThreadMessage } from "@/lib/types";

const MESSAGES: ThreadMessage[] = [
  {
    id: "m1",
    sent_at: "2026-08-10T12:00:00Z",
    sender: "Alice <alice@stripe.com>",
    snippet: null,
    body_text: null,
    body_html: null,
  },
];

function replySent(overrides?: Partial<ReplySent>): ReplySent {
  return {
    thread_id: "t1",
    message: {
      id: "msg-1",
      sent_at: "2026-08-10T12:05:00Z",
      sender: "operator@gmail.com",
      recipient: ["alice@stripe.com"],
      cc: null,
      snippet: "hi",
      body_text: "hi",
      body_html: null,
    },
    replied_at: "2026-08-10T12:05:00Z",
    resolved_action_items: 0,
    ...overrides,
  };
}

function Harness({
  initialOpen = true,
  onSent = vi.fn(),
  onRefetchReplyState = async () => ({ ok: true, repliedAt: null }),
}: {
  initialOpen?: boolean;
  onSent?: (threadId: string, r: ReplySent) => void;
  onRefetchReplyState?: (threadId: string) => Promise<ReplyStateRefetch>;
}) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <ReplyComposer
      threadId="t1"
      provider="gmail"
      repliedAt={null}
      messages={MESSAGES}
      selfAddress="operator@gmail.com"
      open={open}
      onOpenChange={setOpen}
      onSent={onSent}
      onRefetchReplyState={onRefetchReplyState}
    />
  );
}

function setTextareaValue(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    "value",
  )!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
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
  vi.mocked(sendReply).mockReset();
  vi.mocked(draftReply).mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("ReplyComposer collapse/expand", () => {
  it("starts collapsed behind a Reply button, and expanding shows the textarea", async () => {
    await act(async () => {
      root.render(<Harness initialOpen={false} />);
    });
    expect(container.querySelector("textarea")).toBeNull();

    act(() => {
      findButton(container, "Reply").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(container.querySelector("textarea")).not.toBeNull();
  });
});

describe("ReplyComposer send", () => {
  it("sends the trimmed body on Ctrl+Enter and reports the result", async () => {
    const sent = replySent();
    vi.mocked(sendReply).mockResolvedValue(sent);
    const onSent = vi.fn();

    await act(async () => {
      root.render(<Harness onSent={onSent} />);
    });

    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "  Sounds good, thanks!  "));

    await act(async () => {
      textarea.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(sendReply).toHaveBeenCalledWith("t1", {
      bodyText: "Sounds good, thanks!",
      replyAll: false,
      expectedRepliedAt: null,
      overrideAttemptId: null,
    });
    expect(onSent).toHaveBeenCalledWith("t1", sent);
    // Collapses back behind the Reply button on success.
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("ignores a second Ctrl+Enter while the first send is still in flight", async () => {
    let resolveSend: (r: ReplySent) => void;
    vi.mocked(sendReply).mockReturnValue(
      new Promise((resolve) => {
        resolveSend = resolve;
      }),
    );

    await act(async () => {
      root.render(<Harness />);
    });
    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "hello"));

    await act(async () => {
      textarea.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
      textarea.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(sendReply).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSend(replySent());
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("on reply_in_flight, offers to abandon and resend with the blocking attempt id", async () => {
    vi.mocked(sendReply)
      .mockRejectedValueOnce(
        new ApiError(409, "A reply to this thread is already in progress.", undefined, "reply_in_flight", {
          attempt_id: "attempt-42",
        }),
      )
      .mockResolvedValueOnce(replySent());
    const onSent = vi.fn();

    await act(async () => {
      root.render(<Harness onSent={onSent} />);
    });
    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "hello again"));

    await act(async () => {
      findButton(container, "Send").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    const abandonBtn = findButton(container, "Abandon and send anyway");

    await act(async () => {
      abandonBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(sendReply).toHaveBeenLastCalledWith("t1", {
      bodyText: "hello again",
      replyAll: false,
      expectedRepliedAt: null,
      overrideAttemptId: "attempt-42",
    });
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("clears the reply_in_flight banner when the abandon-and-resend hits a different error", async () => {
    vi.mocked(sendReply)
      .mockRejectedValueOnce(
        new ApiError(409, "A reply to this thread is already in progress.", undefined, "reply_in_flight", {
          attempt_id: "attempt-42",
        }),
      )
      .mockRejectedValueOnce(
        new ApiError(429, "Too many replies sent recently.", undefined, "rate_limited"),
      );

    await act(async () => {
      root.render(<Harness />);
    });
    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "hello again"));

    await act(async () => {
      findButton(container, "Send").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    const abandonBtn = findButton(container, "Abandon and send anyway");

    await act(async () => {
      abandonBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    // The stale "abandon and resend" prompt from the first failure must not
    // linger over a second failure with a completely different cause.
    expect(
      Array.from(container.querySelectorAll("button")).some((b) =>
        b.textContent?.includes("Abandon and send anyway"),
      ),
    ).toBe(false);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Too many replies sent recently",
    );
  });
});

describe("ReplyComposer reply_state_stale recovery", () => {
  it("offers Send anyway once a successful refetch confirms the fresh replied_at", async () => {
    vi.mocked(sendReply).mockRejectedValueOnce(
      new ApiError(409, "This thread changed since you opened it.", undefined, "reply_state_stale"),
    );
    const onRefetchReplyState = vi.fn(async () => ({
      ok: true as const,
      repliedAt: "2026-08-13T12:00:00Z",
    }));

    await act(async () => {
      root.render(<Harness onRefetchReplyState={onRefetchReplyState} />);
    });
    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "hello"));

    await act(async () => {
      findButton(container, "Send").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(findButton(container, "Send anyway")).toBeTruthy();
  });

  it("shows a plain retryable error, not Send anyway, when the refetch itself fails", async () => {
    vi.mocked(sendReply).mockRejectedValueOnce(
      new ApiError(409, "This thread changed since you opened it.", undefined, "reply_state_stale"),
    );
    const onRefetchReplyState = vi.fn(async () => ({ ok: false as const }));

    await act(async () => {
      root.render(<Harness onRefetchReplyState={onRefetchReplyState} />);
    });
    const textarea = container.querySelector("textarea")!;
    act(() => setTextareaValue(textarea, "hello"));

    await act(async () => {
      findButton(container, "Send").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    // A failed refetch must never enable Send anyway -- that would resubmit
    // with a null fence and bypass reply_state_stale on a mere network blip.
    expect(
      Array.from(container.querySelectorAll("button")).some((b) =>
        b.textContent?.includes("Send anyway"),
      ),
    ).toBe(false);
    expect(container.querySelector('[role="alert"]')).toBeTruthy();
  });
});

describe("ReplyComposer draft with AI", () => {
  it("disables the button with the server's reason once the draft endpoint 409s", async () => {
    vi.mocked(draftReply).mockRejectedValue(
      new ApiError(409, "No LLM credential is configured for drafting replies.", undefined, "reply_draft_credential_missing"),
    );

    await act(async () => {
      root.render(<Harness />);
    });
    const draftBtn = findButton(container, "Draft with AI");

    await act(async () => {
      draftBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(draftBtn.disabled).toBe(true);
    expect(draftBtn.title).toBe("No LLM credential is configured for drafting replies.");
  });

  it("fills the body from the draft on success", async () => {
    vi.mocked(draftReply).mockResolvedValue({
      draft_text: "Thanks, sounds good.",
      provider: "openai",
      model: "gpt-4o-mini",
    });

    await act(async () => {
      root.render(<Harness />);
    });
    const draftBtn = findButton(container, "Draft with AI");

    await act(async () => {
      draftBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    const textarea = container.querySelector("textarea")!;
    expect(textarea.value).toBe("Thanks, sounds good.");
  });

  it("leaves the button enabled and shows a clearable message on reply_draft_failed", async () => {
    vi.mocked(draftReply)
      .mockRejectedValueOnce(
        new ApiError(502, "Couldn't generate a draft reply.", undefined, "reply_draft_failed"),
      )
      .mockResolvedValueOnce({
        draft_text: "Second try worked.",
        provider: "openai",
        model: "gpt-4o-mini",
      });

    await act(async () => {
      root.render(<Harness />);
    });
    const draftBtn = findButton(container, "Draft with AI");

    await act(async () => {
      draftBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    // reply_draft_failed is retryable -- the button must stay enabled, unlike
    // the credential codes above.
    expect(draftBtn.disabled).toBe(false);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Couldn't generate a draft reply",
    );

    await act(async () => {
      draftBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    const textarea = container.querySelector("textarea")!;
    expect(textarea.value).toBe("Second try worked.");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});
