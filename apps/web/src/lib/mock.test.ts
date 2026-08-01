import { describe, expect, it } from "vitest";

import {
  mockActions,
  mockBackfillActions,
  mockCounts,
  mockDeleteConnection,
  mockIngest,
  mockListConnections,
  mockSearch,
  mockSetActionStatus,
  mockTriage,
} from "./mock";
import { ApiError } from "./api";

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

  it("keeps triage in recency order — date headers and account sort rely on it", () => {
    const { items } = mockTriage("all", 10_000);
    expect(items.length).toBeGreaterThan(0);
    for (let i = 1; i < items.length; i++) {
      // ISO-8601 strings compare correctly as strings
      expect(items[i].last_message_at! <= items[i - 1].last_message_at!).toBe(true);
    }
  });

  it("scopes triage, search, and counts to one account, keeping counts.all consistent", () => {
    const [acct] = mockListConnections();
    const { items } = mockTriage("all", 500, 0, acct.id);
    expect(items.length).toBeGreaterThan(0);
    expect(items.every((i) => i.account_email === acct.email_address)).toBe(true);

    expect(mockCounts(acct.id).counts.all).toBe(items.length);

    const { items: searchItems } = mockSearch("re:", 500, acct.id);
    expect(searchItems.every((i) => i.account_email === acct.email_address)).toBe(true);
  });

  it("self-scopes an unknown/disconnected account id to empty results, never throws", () => {
    expect(mockTriage("all", 50, 0, "not-a-real-id").items).toEqual([]);
    expect(mockCounts("not-a-real-id").counts.all).toBe(0);
    expect(mockSearch("re:", 50, "not-a-real-id").items).toEqual([]);
  });

  it("carries the cross-account agenda counts alongside bucket counts, unaffected by accountId", () => {
    const [acct] = mockListConnections();
    const withAccount = mockCounts(acct.id);
    const withoutAccount = mockCounts(null);
    expect(withAccount.actions).toEqual(withoutAccount.actions);
    expect(withAccount.actions?.open).toBeGreaterThan(0);
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

});

describe("mock agenda", () => {
  it("spans overdue, due-today, and no-deadline items among the open board", () => {
    const { items } = mockActions("open", 200);
    const now = Date.now();
    expect(items.some((a) => a.due_at !== null && new Date(a.due_at).getTime() < now)).toBe(true);
    expect(
      items.some((a) => a.due_at !== null && new Date(a.due_at).getTime() >= now),
    ).toBe(true);
    expect(items.some((a) => a.due_at === null)).toBe(true);
  });

  it("includes a low-confidence item for the 'unverified' treatment", () => {
    const { items } = mockActions("open", 200);
    expect(items.some((a) => a.source_confidence !== null && a.source_confidence < 0.6)).toBe(
      true,
    );
  });

  it("carries two items sourced from the same thread, independently addressable by id", () => {
    const { items } = mockActions("open", 200);
    const byThread = new Map<string, number>();
    for (const item of items) {
      byThread.set(item.thread_id, (byThread.get(item.thread_id) ?? 0) + 1);
    }
    const sharedThreads = [...byThread.entries()].filter(([, count]) => count > 1);
    expect(sharedThreads.length).toBeGreaterThanOrEqual(1);

    const [threadId] = sharedThreads[0];
    const shared = items.filter((a) => a.thread_id === threadId);
    expect(shared.length).toBe(2);
    expect(shared[0].id).not.toBe(shared[1].id);
  });

  it("filters the board by status, keeping counts fixed to the open/overdue tally", () => {
    const open = mockActions("open");
    const done = mockActions("done");
    expect(open.items.every((a) => a.status === "open")).toBe(true);
    expect(done.items.length).toBeGreaterThan(0);
    expect(done.items.every((a) => a.status === "done")).toBe(true);
    // Counts don't change with the status filter -- same contract as the API.
    expect(open.counts).toEqual(done.counts);
  });

  it("respects the limit param", () => {
    expect(mockActions("open", 1).items).toHaveLength(1);
  });

  it("round-trips a status change, including status_at clearing on reopen", () => {
    const [item] = mockActions("open").items;
    const dismissed = mockSetActionStatus(item.id, "dismissed");
    expect(dismissed.status).toBe("dismissed");
    expect(dismissed.status_at).not.toBeNull();
    expect(mockActions("dismissed").items.some((a) => a.id === item.id)).toBe(true);

    const reopened = mockSetActionStatus(item.id, "open");
    expect(reopened.status).toBe("open");
    expect(reopened.status_at).toBeNull();
    expect(mockActions("open").items.some((a) => a.id === item.id)).toBe(true);
  });

  it("throws a 404 ApiError for an unknown action id", () => {
    expect(() => mockSetActionStatus("not-a-real-id", "done")).toThrow(ApiError);
  });

  it("backfill always reports a queued mock task", () => {
    const result = mockBackfillActions();
    expect(result.status).toBe("queued");
    expect(result.task_id).toMatch(/^mock-actions-task-/);
  });
});

// Deletes a connection for real — must run last, after every test above that
// relies on the full seed data (both accounts' mail AND agenda rows).
describe("mock delete connection", () => {
  it("round-trips listConnections/deleteConnection, cascading the removed account's mail and agenda rows", () => {
    const before = mockListConnections();
    expect(before.length).toBeGreaterThanOrEqual(2);
    const target = before[0];
    const actionsForTarget = mockActions("open", 500).items.filter(
      (a) => a.account_email === target.email_address,
    );
    expect(actionsForTarget.length).toBeGreaterThan(0);

    expect(mockDeleteConnection(target.id)).toBe(true);

    const after = mockListConnections();
    expect(after.length).toBe(before.length - 1);
    expect(after.some((c) => c.id === target.id)).toBe(false);

    // Mirrors the server: dropping a connection takes its synced mail with it.
    const { items } = mockTriage("all", 500);
    expect(items.every((i) => i.account_email !== target.email_address)).toBe(true);

    // ...and its agenda rows too — otherwise the board shows obligations for
    // an account (and threads) that no longer exist.
    const remainingActions = mockActions("open", 500).items;
    expect(remainingActions.every((a) => a.account_email !== target.email_address)).toBe(true);

    // Re-deleting the same id (or one that never existed) is a no-op 404, not
    // a crash — the caller (api.ts) turns a `false` here into an ApiError.
    expect(mockDeleteConnection(target.id)).toBe(false);
    expect(mockDeleteConnection("not-a-real-id")).toBe(false);
  });
});
