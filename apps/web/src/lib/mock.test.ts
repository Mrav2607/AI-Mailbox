import { describe, expect, it } from "vitest";

import { mockDeleteConnection, mockListConnections, mockTriage } from "./mock";

describe("mock accounts", () => {
  it("tags every triage item with one of the connected accounts' emails", () => {
    const emails = new Set(mockListConnections().map((c) => c.email_address));
    const { items } = mockTriage("all", 50);
    expect(items.length).toBeGreaterThan(0);
    for (const item of items) {
      expect(emails.has(item.account_email)).toBe(true);
    }
  });

  it("round-trips listConnections/deleteConnection, cascading the removed account's mail", () => {
    const before = mockListConnections();
    expect(before.length).toBeGreaterThanOrEqual(2);
    const target = before[0];

    expect(mockDeleteConnection(target.id)).toBe(true);

    const after = mockListConnections();
    expect(after.length).toBe(before.length - 1);
    expect(after.some((c) => c.id === target.id)).toBe(false);

    // Mirrors the server: dropping a connection takes its synced mail with it.
    const { items } = mockTriage("all", 500);
    expect(items.every((i) => i.account_email !== target.email_address)).toBe(true);

    // Re-deleting the same id (or one that never existed) is a no-op 404, not
    // a crash — the caller (api.ts) turns a `false` here into an ApiError.
    expect(mockDeleteConnection(target.id)).toBe(false);
    expect(mockDeleteConnection("not-a-real-id")).toBe(false);
  });
});
