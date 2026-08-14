import { describe, expect, it } from "vitest";

import { approximateRecipients, recipientLine } from "./ReplyComposer";
import type { ThreadMessage } from "@/lib/types";

function msg(patch: Partial<ThreadMessage>): ThreadMessage {
  return {
    id: "m1",
    sent_at: "2026-08-10T12:00:00Z",
    sender: null,
    snippet: null,
    body_text: null,
    body_html: null,
    ...patch,
  };
}

describe("approximateRecipients", () => {
  it("excludes the self address, case-insensitively", () => {
    const names = approximateRecipients(
      [
        msg({ id: "m1", sender: "Operator <Operator@Gmail.com>", sent_at: "2026-08-10T12:00:00Z" }),
        msg({ id: "m2", sender: "Alice <alice@stripe.com>", sent_at: "2026-08-10T11:00:00Z" }),
      ],
      "operator@gmail.com",
    );
    expect(names).toEqual(["Alice"]);
  });

  it("orders by sent_at descending regardless of array order", () => {
    const names = approximateRecipients(
      [
        msg({ id: "older", sender: "Alice <alice@stripe.com>", sent_at: "2026-08-10T09:00:00Z" }),
        msg({ id: "newer", sender: "Bob <bob@figma.com>", sent_at: "2026-08-10T11:00:00Z" }),
      ],
      "operator@gmail.com",
    );
    expect(names).toEqual(["Bob", "Alice"]);
  });

  it("dedupes repeat senders across the thread, keeping the newest occurrence's name", () => {
    const names = approximateRecipients(
      [
        msg({ id: "m1", sender: "Alice <alice@stripe.com>", sent_at: "2026-08-10T10:00:00Z" }),
        msg({ id: "m2", sender: "alice@stripe.com", sent_at: "2026-08-10T09:00:00Z" }),
      ],
      "operator@gmail.com",
    );
    expect(names).toEqual(["Alice"]);
  });

  it("falls back to the address's local part when there's no display name", () => {
    const names = approximateRecipients(
      [msg({ sender: "alice@stripe.com" })],
      "operator@gmail.com",
    );
    expect(names).toEqual(["alice"]);
  });

  it("returns an empty list when every message is from self (or unparseable)", () => {
    const names = approximateRecipients(
      [msg({ sender: "operator@gmail.com" }), msg({ sender: null })],
      "operator@gmail.com",
    );
    expect(names).toEqual([]);
  });
});

describe("recipientLine", () => {
  it("says recipients are computed at send time when nothing's known yet", () => {
    expect(recipientLine([], false)).toBe("Recipients are computed when you send.");
  });

  it("shows just the one target for a plain reply", () => {
    expect(recipientLine(["Alice", "Bob"], false)).toBe("To Alice");
  });

  it("counts the rest for reply-all", () => {
    expect(recipientLine(["Alice", "Bob", "Carol", "Dave"], true)).toBe(
      "To Alice · +3 others on reply-all",
    );
  });

  it("uses the singular for exactly one other participant", () => {
    expect(recipientLine(["Alice", "Bob"], true)).toBe("To Alice · +1 other on reply-all");
  });

  it("reply-all with only one known participant reads the same as a plain reply", () => {
    expect(recipientLine(["Alice"], true)).toBe("To Alice");
  });
});
