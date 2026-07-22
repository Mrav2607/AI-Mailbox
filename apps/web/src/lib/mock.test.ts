import { describe, expect, it } from "vitest";

import {
  mockCounts,
  mockDeleteConnection,
  mockIngest,
  mockListConnections,
  mockSearch,
  mockTriage,
} from "./mock";

describe("mock accounts", () => {
  it("tags every triage item with one of the connected accounts' emails", () => {
    const emails = new Set(mockListConnections().map((c) => c.email_address));
    const { items } = mockTriage("all", 50);
    expect(items.length).toBeGreaterThan(0);
    for (const item of items) {
      expect(emails.has(item.account_email)).toBe(true);
    }
  });

  it("pages disjointly and concatenates to the whole bucket, with a short last page", () => {
    const pageSize = 200;
    const first = mockTriage("all", pageSize, 0);
    const second = mockTriage("all", pageSize, pageSize);
    const third = mockTriage("all", pageSize, pageSize * 2);
    expect(first.items).toHaveLength(pageSize);
    expect(second.items).toHaveLength(pageSize);
    expect(third.items.length).toBeGreaterThan(0);
    expect(third.items.length).toBeLessThan(pageSize);

    const whole = mockTriage("all", 10_000, 0).items;
    const concatenated = [...first.items, ...second.items, ...third.items];
    expect(concatenated.map((i) => i.thread_id)).toEqual(whole.map((i) => i.thread_id));

    // Disjoint: no thread_id shows up on more than one page.
    const ids = new Set(concatenated.map((i) => i.thread_id));
    expect(ids.size).toBe(concatenated.length);
  });

  it("scopes triage, search, and counts to one account, keeping counts.all consistent", () => {
    const [acct] = mockListConnections();
    const { items } = mockTriage("all", 500, 0, acct.id);
    expect(items.length).toBeGreaterThan(0);
    expect(items.every((i) => i.account_email === acct.email_address)).toBe(true);

    expect(mockCounts(acct.id).all).toBe(items.length);

    const { items: searchItems } = mockSearch("re:", 500, acct.id);
    expect(searchItems.every((i) => i.account_email === acct.email_address)).toBe(true);
  });

  it("self-scopes an unknown/disconnected account id to empty results, never throws", () => {
    expect(mockTriage("all", 50, 0, "not-a-real-id").items).toEqual([]);
    expect(mockCounts("not-a-real-id").all).toBe(0);
    expect(mockSearch("re:", 50, "not-a-real-id").items).toEqual([]);
  });

  it("groups items by account email when sort is 'account'", () => {
    const { items } = mockTriage("all", 500, 0, null, "account");
    const emails = items.map((i) => i.account_email);

    // Each account's rows are contiguous — as many "switches" between
    // consecutive emails as there are accounts minus one.
    let switches = 0;
    for (let i = 1; i < emails.length; i++) {
      if (emails[i] !== emails[i - 1]) switches += 1;
    }
    expect(switches).toBe(new Set(emails).size - 1);
    expect(emails).toEqual([...emails].sort((a, b) => a.localeCompare(b)));
  });

  it("targets mockIngest at a subset of accounts", () => {
    const [acct] = mockListConnections();
    const before = mockTriage("all", 10_000, 0, acct.id).items.length;

    const created = mockIngest([acct.id]);
    expect(created).toBe(2);

    const after = mockTriage("all", 10_000, 0, acct.id).items;
    expect(after.length).toBe(before + 2);
    expect(after.slice(0, 2).every((i) => i.account_email === acct.email_address)).toBe(true);
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
