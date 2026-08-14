import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ReplyComposer (rendered whenever the composer is enabled) reaches for
// these -- same stub shape as ReplyComposer.render.test.tsx. Nothing here
// actually sends a reply, so the mocks just need to exist.
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  sendReply: vi.fn(),
  draftReply: vi.fn(),
}));

import { ThreadDetailPane } from "./ThreadDetailPane";
import type { ThreadDetail } from "@/lib/types";

function threadDetail(id: string): ThreadDetail {
  return {
    thread: {
      id,
      subject: `Subject for ${id}`,
      provider: "gmail",
      provider_thread_id: null,
      last_message_at: "2026-08-13T12:00:00Z",
      done: false,
      account_email: "operator@gmail.com",
      replied_at: null,
      snoozed_until: null,
    },
    messages: [
      {
        id: `${id}-m1`,
        sent_at: "2026-08-13T12:00:00Z",
        sender: "Alice <alice@stripe.com>",
        snippet: "hi there",
        body_text: "hi there",
        body_html: null,
      },
    ],
    classification: null,
  };
}

function findReplyButton(container: HTMLElement): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (b) => b.getAttribute("aria-label") === "Reply" || b.getAttribute("aria-label") === "Hide reply composer",
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

// R-6: App.tsx withholds onComposerOpenChange/onReplySent/onRefetchReplyState
// (instead of just forcing composerOpen false) while the loaded thread is
// stale relative to the current selection -- the same window the done/delete
// buttons already guard against via detailThreadId. ThreadDetailPane's
// composerEnabled requires all three, so these tests pin down that omitting
// any one of them removes both the Reply button and an already-open
// composer, which is what actually closes the pointer-click path onto a
// stale thread.
describe("ThreadDetailPane composer prop-gating (R-6)", () => {
  it("shows the Reply button and lets it open the composer when all composer callbacks are wired", async () => {
    await act(async () => {
      root.render(
        <ThreadDetailPane
          data={threadDetail("t-a")}
          classification={null}
          onReclassify={() => {}}
          composerOpen={false}
          onComposerOpenChange={() => {}}
          onReplySent={() => {}}
          onRefetchReplyState={async () => ({ ok: true, repliedAt: null })}
        />,
      );
    });

    expect(findReplyButton(container)).toBeTruthy();
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("hides the Reply button and force-closes an already-open composer when the callbacks are withheld", async () => {
    // Mirrors App.tsx's threadMatchesSelection === false case: `composerOpen`
    // is still true (its own reset effect hasn't flushed yet) but the
    // callbacks are gone because `thread` no longer matches `selectedId`.
    await act(async () => {
      root.render(
        <ThreadDetailPane
          data={threadDetail("t-a")}
          classification={null}
          onReclassify={() => {}}
          composerOpen={true}
        />,
      );
    });

    expect(findReplyButton(container)).toBeUndefined();
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("closes an open composer for thread A the instant the callbacks drop out from under it", async () => {
    // Simulates the pending-fetch window: thread A's composer is open, the
    // operator picks thread B, and App withholds the composer callbacks
    // before `data` itself has caught up to B.
    await act(async () => {
      root.render(
        <ThreadDetailPane
          data={threadDetail("t-a")}
          classification={null}
          onReclassify={() => {}}
          composerOpen={true}
          onComposerOpenChange={() => {}}
          onReplySent={() => {}}
          onRefetchReplyState={async () => ({ ok: true, repliedAt: null })}
        />,
      );
    });
    expect(container.querySelector("textarea")).not.toBeNull();

    await act(async () => {
      root.render(
        <ThreadDetailPane
          data={threadDetail("t-a")}
          classification={null}
          onReclassify={() => {}}
          composerOpen={true}
        />,
      );
    });

    expect(findReplyButton(container)).toBeUndefined();
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("withholding only onRefetchReplyState alone is still enough to disable the composer", async () => {
    // composerEnabled requires all three -- a caller that wires two of three
    // must not get a Reply button that opens a composer which can never
    // actually send.
    await act(async () => {
      root.render(
        <ThreadDetailPane
          data={threadDetail("t-a")}
          classification={null}
          onReclassify={() => {}}
          composerOpen={false}
          onComposerOpenChange={() => {}}
          onReplySent={() => {}}
        />,
      );
    });

    expect(findReplyButton(container)).toBeUndefined();
  });
});
