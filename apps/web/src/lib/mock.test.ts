import { afterEach, describe, expect, it } from "vitest";

import {
  mockActions,
  mockBackfillActions,
  mockCounts,
  mockDeleteConnection,
  mockDeleteLlmSettings,
  mockForceLabelSyncEnableFailure,
  mockGetClassifierMix,
  mockGetLlmSettings,
  mockGetLlmUsage,
  mockIngest,
  mockListConnections,
  mockPutLlmSettings,
  mockSearch,
  mockSetActionStatus,
  mockSetDone,
  mockSnoozeThread,
  mockTestLlmSettings,
  mockThread,
  mockTriage,
  mockUpdateConnection,
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

  it("has enough generated open rows to genuinely page past a 100-row limit", () => {
    expect(mockActions("open", 10_000).items.length).toBeGreaterThan(100);
  });

  it("pages the open board to completion: full pages carry a cursor, the last short page carries none", () => {
    const pageSize = 100;
    const first = mockActions("open", pageSize);
    expect(first.items).toHaveLength(pageSize);
    expect(first.next_cursor).not.toBeNull();

    const second = mockActions("open", pageSize, first.next_cursor!);
    expect(second.next_cursor).toBeNull(); // the walk ends here — not enough rows for a third full page
    expect(second.items.length).toBeGreaterThan(0);
    expect(second.items.length).toBeLessThan(pageSize);

    // Disjoint and order-preserving: concatenating the two pages reproduces
    // a single unpaged request over the same rows, no skip, no duplicate.
    const whole = mockActions("open", 10_000).items;
    const concatenated = [...first.items, ...second.items];
    expect(concatenated.map((a) => a.id)).toEqual(whole.map((a) => a.id));
    expect(new Set(concatenated.map((a) => a.id)).size).toBe(concatenated.length);
  });

  it("mints a cursor a status-mismatched call can't reuse, mirroring the API's own contract", () => {
    // The mock doesn't validate this (only the real route does), but a
    // caller switching status mid-walk still starts over cleanly rather than
    // silently mixing pages from two different filtered views.
    const openPage = mockActions("open", 100);
    const dismissedWithOpenCursor = mockActions("dismissed", 100, openPage.next_cursor ?? undefined);
    expect(dismissedWithOpenCursor.items.every((a) => a.status === "dismissed")).toBe(true);
  });
});

describe("mock llm usage", () => {
  it("defaults to a 30-day window of sparse days, oldest first", () => {
    const usage = mockGetLlmUsage();
    expect(usage.window_days).toBe(30);
    // Deliberately sparse: the real API only returns days that actually have
    // rows, so idle days are ABSENT rather than zero. A dense mock would hide
    // the gap-filling fillDailySeries has to do before anything is charted.
    expect(usage.daily.length).toBeGreaterThan(0);
    expect(usage.daily.length).toBeLessThan(30);
    for (let i = 1; i < usage.daily.length; i++) {
      expect(usage.daily[i].date > usage.daily[i - 1].date).toBe(true);
    }
  });

  it("keeps every day inside the requested window", () => {
    const usage = mockGetLlmUsage(7);
    expect(usage.window_days).toBe(7);
    expect(usage.daily.length).toBeGreaterThan(0);
    expect(usage.daily.length).toBeLessThanOrEqual(7);

    const oldest = new Date();
    oldest.setUTCDate(oldest.getUTCDate() - 6);
    const oldestDay = oldest.toISOString().slice(0, 10);
    const today = new Date().toISOString().slice(0, 10);
    for (const point of usage.daily) {
      expect(point.date >= oldestDay).toBe(true);
      expect(point.date <= today).toBe(true);
    }
  });

  it("has non-zero calls and tokens, so the panel actually demos something", () => {
    const usage = mockGetLlmUsage();
    expect(usage.totals.calls).toBeGreaterThan(0);
    expect(usage.totals.total_tokens).toBeGreaterThan(0);
  });

  it("sums by_stage into totals exactly", () => {
    const usage = mockGetLlmUsage();
    const summed = usage.by_stage.reduce(
      (acc, s) => ({
        calls: acc.calls + s.calls,
        calls_with_total_tokens: acc.calls_with_total_tokens + s.calls_with_total_tokens,
        prompt_tokens: acc.prompt_tokens + s.prompt_tokens,
        completion_tokens: acc.completion_tokens + s.completion_tokens,
        total_tokens: acc.total_tokens + s.total_tokens,
      }),
      { calls: 0, calls_with_total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    );
    expect(summed).toEqual(usage.totals);
  });

  it("sums the daily series' calls/tokens into the same totals", () => {
    const usage = mockGetLlmUsage();
    const dailyCalls = usage.daily.reduce((sum, d) => sum + d.calls, 0);
    const dailyTokens = usage.daily.reduce((sum, d) => sum + d.total_tokens, 0);
    expect(dailyCalls).toBe(usage.totals.calls);
    expect(dailyTokens).toBe(usage.totals.total_tokens);
  });

  it("some days have calls_with_total_tokens below calls -- the partial-reporting case", () => {
    const usage = mockGetLlmUsage();
    const classification = usage.by_stage.find((s) => s.stage === "classification")!;
    expect(classification.calls_with_total_tokens).toBeLessThan(classification.calls);
  });

  it("attributes every call to the currently configured provider", () => {
    const usage = mockGetLlmUsage();
    expect(usage.by_provider).toHaveLength(1);
    expect(usage.by_provider[0].provider).toBe(mockGetLlmSettings().provider);
    expect(usage.by_provider[0].calls).toBe(usage.totals.calls);
  });
});

describe("mock classifier mix", () => {
  it("returns a non-empty mix with no duplicate kinds and only positive counts", () => {
    const { classifier_mix } = mockGetClassifierMix();
    expect(classifier_mix.length).toBeGreaterThan(0);
    const kinds = classifier_mix.map((entry) => entry.kind);
    expect(new Set(kinds).size).toBe(kinds.length);
    for (const entry of classifier_mix) {
      expect(entry.count).toBeGreaterThan(0);
    }
  });

  it("never includes 'unknown' -- the demo has no unclassified rows to show", () => {
    const { classifier_mix } = mockGetClassifierMix();
    expect(classifier_mix.some((entry) => entry.kind === "unknown")).toBe(false);
  });
});

// Deletes a connection for real — must run last, after every test above that
// relies on the full seed data (both accounts' mail AND agenda rows).
// Unsnoozes anything a test snoozed, so leftover module-level SNOOZED state
// can't bleed into a later block's bucket-count assertions (same reasoning
// as the "mock llm settings" block's placement comment below).
describe("mock snooze", () => {
  const touched: string[] = [];
  afterEach(() => {
    for (const id of touched.splice(0)) {
      try {
        mockSnoozeThread(id, null);
      } catch {
        // Already unsnoozed (or never snoozed) by the test itself — fine.
      }
    }
  });

  function pickThreadId(): string {
    const { items } = mockTriage("all", 1);
    const id = items[0].thread_id;
    touched.push(id);
    return id;
  }

  it("moves a thread into the snoozed bucket and out of 'all', with snoozed_until exposed", () => {
    const id = pickThreadId();
    const until = new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString();

    const res = mockSnoozeThread(id, until);
    expect(res.thread_id).toBe(id);
    expect(res.snoozed_until).toBe(until);
    expect(res.snoozed_at).not.toBeNull();

    expect(mockTriage("all", 10_000).items.some((i) => i.thread_id === id)).toBe(false);
    const snoozedItems = mockTriage("snoozed", 10_000).items;
    const row = snoozedItems.find((i) => i.thread_id === id);
    expect(row).toBeDefined();
    expect(row!.snoozed_until).toBe(until);
  });

  it("counts.snoozed and counts.all both reflect an active snooze", () => {
    const id = pickThreadId();
    const before = mockCounts(null).counts;

    mockSnoozeThread(id, new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString());
    const after = mockCounts(null).counts;

    expect(after.snoozed).toBe(before.snoozed + 1);
    expect(after.all).toBe(before.all - 1);
  });

  it("sorts the snoozed bucket by soonest wake first", () => {
    const { items } = mockTriage("all", 3);
    const [a, b, c] = items.map((i) => i.thread_id);
    touched.push(a, b, c);
    mockSnoozeThread(a, new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString());
    mockSnoozeThread(b, new Date(Date.now() + 1 * 60 * 60 * 1000).toISOString());
    mockSnoozeThread(c, new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString());

    const order = mockTriage("snoozed", 10_000)
      .items.map((i) => i.thread_id)
      .filter((id) => [a, b, c].includes(id));
    expect(order).toEqual([b, c, a]);
  });

  it("breaks a tied wake time with id DESC, not insertion order (S-4)", () => {
    const { items } = mockTriage("all", 3);
    const [a, b, c] = items.map((i) => i.thread_id);
    touched.push(a, b, c);
    const until = new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString();
    // Snooze in an order that doesn't match id-sorted order, so a bug that
    // falls back to insertion order would produce a different sequence than
    // the id-DESC tiebreak.
    mockSnoozeThread(b, until);
    mockSnoozeThread(a, until);
    mockSnoozeThread(c, until);

    const order = mockTriage("snoozed", 10_000)
      .items.map((i) => i.thread_id)
      .filter((id) => [a, b, c].includes(id));
    expect(order).toEqual([a, b, c].sort((x, y) => y.localeCompare(x)));
  });

  it("clears the snooze on until: null, restoring the thread to its open bucket", () => {
    const id = pickThreadId();
    mockSnoozeThread(id, new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString());
    expect(mockTriage("snoozed", 10_000).items.some((i) => i.thread_id === id)).toBe(true);

    const res = mockSnoozeThread(id, null);
    expect(res.snoozed_until).toBeNull();
    expect(res.snoozed_at).toBeNull();
    expect(mockTriage("snoozed", 10_000).items.some((i) => i.thread_id === id)).toBe(false);
    expect(mockTriage("all", 10_000).items.some((i) => i.thread_id === id)).toBe(true);
  });

  it("rejects an until less than the minimum lead with snooze_too_soon", () => {
    const id = pickThreadId();
    let caught: ApiError | undefined;
    try {
      mockSnoozeThread(id, new Date(Date.now() + 30 * 1000).toISOString());
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect(caught!.status).toBe(422);
    expect(caught!.code).toBe("snooze_too_soon");
  });

  it("rejects an until past the maximum lead with snooze_too_far", () => {
    const id = pickThreadId();
    let caught: ApiError | undefined;
    try {
      mockSnoozeThread(id, new Date(Date.now() + 400 * 24 * 60 * 60 * 1000).toISOString());
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect(caught!.status).toBe(422);
    expect(caught!.code).toBe("snooze_too_far");
  });

  it("rejects a naive datetime string (no Z/offset) with naive_datetime", () => {
    // `new Date("...T08:00:00")` parses fine as LOCAL time in JS -- the real
    // API 422s this exact shape as naive_datetime, so the mock has to match.
    const id = pickThreadId();
    let caught: ApiError | undefined;
    try {
      mockSnoozeThread(id, "2026-09-01T08:00:00");
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect(caught!.status).toBe(422);
    expect(caught!.code).toBe("naive_datetime");
  });

  it("accepts an until carrying a numeric UTC offset, not just Z", () => {
    const id = pickThreadId();
    const until = new Date(Date.now() + 3 * 60 * 60 * 1000);
    const offsetString = until.toISOString().replace("Z", "+00:00");

    const res = mockSnoozeThread(id, offsetString);
    expect(res.snoozed_until).toBe(until.toISOString());
  });

  it("404s snoozing a thread id that doesn't exist", () => {
    expect(() =>
      mockSnoozeThread("not-a-real-id", new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString()),
    ).toThrow(ApiError);
  });

  it("snoozing clears done (mutual exclusivity), and marking done clears an active snooze", () => {
    const id = pickThreadId();
    mockSetDone(id, true);
    expect(mockThread(id).thread.done).toBe(true);

    mockSnoozeThread(id, new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString());
    expect(mockThread(id).thread.done).toBe(false);
    expect(mockThread(id).thread.snoozed_until).not.toBeNull();

    mockSetDone(id, true);
    expect(mockThread(id).thread.done).toBe(true);
    expect(mockThread(id).thread.snoozed_until).toBeNull();
    mockSetDone(id, false);
  });

  it("mockThread and mockSearch both expose the projected snoozed_until", () => {
    const id = pickThreadId();
    const until = new Date(Date.now() + 5 * 60 * 60 * 1000).toISOString();
    mockSnoozeThread(id, until);

    expect(mockThread(id).thread.snoozed_until).toBe(until);
    const found = mockSearch("", 10_000).items.find((i) => i.thread_id === id);
    expect(found?.snoozed_until).toBe(until);
  });
});

describe("mock update connection (label sync)", () => {
  it("404s for an unknown connection id", () => {
    expect(() => mockUpdateConnection("not-a-real-id", true)).toThrow(ApiError);
    let caught: ApiError | undefined;
    try {
      mockUpdateConnection("not-a-real-id", true);
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught!.status).toBe(404);
  });

  it("enables sync, seeding a drift count from this account's classified mail, then echoes a repeat", () => {
    const [conn] = mockListConnections();
    expect(conn.label_sync_enabled).toBe(false);

    const enabled = mockUpdateConnection(conn.id, true);
    expect(enabled.label_sync_enabled).toBe(true);
    expect(enabled.label_sync_drift).toEqual(expect.any(Number));
    expect(enabled.label_sync_drift).toBeGreaterThan(0);

    // A repeat of the current state is a pure echo -- same drift, no
    // recomputation -- mirroring the real route's early return.
    const echoed = mockUpdateConnection(conn.id, true);
    expect(echoed).toEqual(enabled);

    // Reset for tests below.
    mockUpdateConnection(conn.id, false);
  });

  it("disables sync, clearing both the flag and the drift count", () => {
    const [conn] = mockListConnections();
    mockUpdateConnection(conn.id, true);
    const disabled = mockUpdateConnection(conn.id, false);
    expect(disabled.label_sync_enabled).toBe(false);
    expect(disabled.label_sync_drift).toBeNull();
  });

  // Every ENABLE-time failure code (plan §3.3) is representable via the
  // forced-failure rig, one-shot -- the retry after "fixing" it succeeds,
  // same as the real reconnect-then-retry / wait-then-retry flows.
  it.each(["missing_scope", "reauth_required", "account_paused", "label_sync_busy"])(
    "surfaces a forced %s failure once, without changing the flag, then succeeds on retry",
    (code) => {
      const [conn] = mockListConnections();
      expect(conn.label_sync_enabled).toBe(false);

      mockForceLabelSyncEnableFailure(conn.id, code);
      let caught: ApiError | undefined;
      try {
        mockUpdateConnection(conn.id, true);
      } catch (e) {
        caught = e as ApiError;
      }
      expect(caught).toBeInstanceOf(ApiError);
      expect(caught!.status).toBe(409);
      expect(caught!.code).toBe(code);
      // The flag never changed on failure.
      expect(mockListConnections()[0].label_sync_enabled).toBe(false);

      // One-shot: the same call now succeeds.
      const retried = mockUpdateConnection(conn.id, true);
      expect(retried.label_sync_enabled).toBe(true);
      mockUpdateConnection(conn.id, false); // reset for the next case
    },
  );
});

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

// Mutates the module-level LLM settings singleton, ending in the
// unconfigured state -- placed last (after the also-destructive delete-
// connection block above) so nothing later in the file can observe a
// mid-round-trip state.
describe("mock llm settings", () => {
  it("starts configured with a demo credential", () => {
    const settings = mockGetLlmSettings();
    expect(settings.configured).toBe(true);
    expect(settings.provider).toBe("openai");
    expect(settings.key_suffix).toBe("sk12");
    expect(settings.last_verified_at).not.toBeNull();
    expect(settings.fallback_active).toBe(false);
    // Opted in on a preset provider from the start, so preview shows the
    // "local model handles most mail" notice on first load.
    expect(settings.classification_byok).toBe(true);
    // A true opt-in, off by default even with classification_byok on.
    expect(settings.classification_fallback_local).toBe(false);
    expect(settings.classifier_uses_llm).toBe(true);
    expect(settings.classifier_backend).toBe("auto");
    expect(settings.classification_eligible).toBe(true);
  });

  it("put derives key_suffix from the last 4 chars and clears last_verified_at", () => {
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_abcd1234",
      model: "llama-3.1-8b-instant",
    });
    expect(updated.configured).toBe(true);
    expect(updated.provider).toBe("groq");
    expect(updated.model).toBe("llama-3.1-8b-instant");
    expect(updated.key_suffix).toBe("1234");
    expect(updated.last_verified_at).toBeNull();
    expect(mockGetLlmSettings()).toEqual(updated);
  });

  it("put leaves classification_byok unchanged when the field is absent", () => {
    // The prior test's PUT didn't touch classification_byok -- still true
    // from the initial demo state.
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_ignored123",
      model: "llama-3.1-8b-instant",
    });
    expect(updated.classification_byok).toBe(true);
    expect(updated.classification_eligible).toBe(true);
  });

  it("put turns classification_byok off on request, dropping eligibility with it", () => {
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_abcd5678",
      model: "llama-3.1-8b-instant",
      classification_byok: false,
    });
    expect(updated.classification_byok).toBe(false);
    expect(updated.classification_eligible).toBe(false);
  });

  it("put leaves classification_fallback_local unchanged when the field is absent", () => {
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_ignored456",
      model: "llama-3.1-8b-instant",
    });
    expect(updated.classification_fallback_local).toBe(false);
  });

  it("put threads classification_fallback_local through on request", () => {
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_abcd9012",
      model: "llama-3.1-8b-instant",
      classification_fallback_local: true,
    });
    expect(updated.classification_fallback_local).toBe(true);
  });

  it("put does not coerce classification_fallback_local off when classification_byok is off", () => {
    // The real API leaves this stored but inert rather than clearing it --
    // the mock mirrors that instead of silently zeroing the flag out.
    const updated = mockPutLlmSettings({
      provider: "groq",
      api_key: "gsk_live_abcd3456",
      model: "llama-3.1-8b-instant",
      classification_byok: false,
    });
    expect(updated.classification_byok).toBe(false);
    expect(updated.classification_fallback_local).toBe(true);
  });

  it("put marks classification_eligible false for a custom provider even with the flag on -- the out-of-band notice state", () => {
    const updated = mockPutLlmSettings({
      provider: "custom",
      api_key: "custom-key-eligible",
      model: "local-model",
      base_url: "https://my-endpoint.example/v1",
      classification_byok: true,
    });
    expect(updated.provider).toBe("custom");
    expect(updated.classification_byok).toBe(true);
    expect(updated.classification_eligible).toBe(false);
  });

  it("put pins a preset's base_url, ignoring any caller-supplied value", () => {
    const updated = mockPutLlmSettings({
      provider: "mistral",
      api_key: "mistral-key-5678",
      model: "mistral-small",
      base_url: "https://not-the-real-endpoint.example",
    });
    expect(updated.base_url).toBe("https://api.mistral.ai/v1");
  });

  it("put pins the anthropic preset's base_url, ignoring any caller-supplied value", () => {
    const updated = mockPutLlmSettings({
      provider: "anthropic",
      api_key: "anthropic-key-5678",
      model: "claude-haiku-4-5",
      base_url: "https://not-the-real-endpoint.example",
    });
    expect(updated.base_url).toBe("https://api.anthropic.com/v1");
  });

  it("put stores a custom base_url as given", () => {
    const updated = mockPutLlmSettings({
      provider: "custom",
      api_key: "custom-key-9999",
      model: "local-model",
      base_url: "https://my-endpoint.example/v1",
    });
    expect(updated.provider).toBe("custom");
    expect(updated.base_url).toBe("https://my-endpoint.example/v1");
  });

  it("test stamps last_verified_at and reports ok with a plausible latency", () => {
    const before = mockGetLlmSettings().last_verified_at;
    const result = mockTestLlmSettings();
    expect(result.ok).toBe(true);
    expect(result.error).toBeNull();
    expect(result.latency_ms).toBeGreaterThan(0);
    const after = mockGetLlmSettings().last_verified_at;
    expect(after).not.toBeNull();
    expect(after).not.toBe(before);
  });

  it("put with an absent api_key preserves the key_suffix and last_verified_at on a flag-only edit", () => {
    const before = mockGetLlmSettings();
    expect(before.last_verified_at).not.toBeNull();
    const updated = mockPutLlmSettings({
      provider: "custom",
      model: "local-model",
      base_url: "https://my-endpoint.example/v1",
      classification_byok: false,
    });
    expect(updated.key_suffix).toBe("9999");
    expect(updated.last_verified_at).toBe(before.last_verified_at);
    expect(updated.classification_byok).toBe(false);
  });

  it("put with a new api_key rotates the key_suffix and clears last_verified_at even when nothing else changed", () => {
    const updated = mockPutLlmSettings({
      provider: "custom",
      api_key: "custom-key-rotated",
      model: "local-model",
      base_url: "https://my-endpoint.example/v1",
    });
    expect(updated.key_suffix).toBe("ated");
    expect(updated.last_verified_at).toBeNull();
  });

  it("delete resets to unconfigured with nulled fields", () => {
    mockDeleteLlmSettings();
    const settings = mockGetLlmSettings();
    expect(settings.configured).toBe(false);
    expect(settings.provider).toBeNull();
    expect(settings.model).toBeNull();
    expect(settings.base_url).toBeNull();
    expect(settings.key_suffix).toBeNull();
    expect(settings.last_verified_at).toBeNull();
    expect(settings.custom_blocked).toBe(false);
    expect(settings.fallback_active).toBe(true);
    // No credential left to opt in -- classification falls back to the
    // deployment default; the effective-backend fields are untouched by
    // deleting a credential.
    expect(settings.classification_byok).toBe(false);
    expect(settings.classification_fallback_local).toBe(false);
    expect(settings.classification_eligible).toBe(false);
    // Preview has no operator key, so removing the credential leaves nothing
    // for the LLM backend to call -- the backfill form reads this to disable
    // that option instead of letting it silently fall back to keyword rules.
    expect(settings.classification_llm_usable).toBe(false);
    expect(settings.classifier_uses_llm).toBe(true);
    expect(settings.classifier_backend).toBe("auto");
  });
});
