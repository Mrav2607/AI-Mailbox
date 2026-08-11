import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { groupActions } from "./agenda";
import type { ActionItem } from "./types";

// This app's tsconfig doesn't pull in Node's ambient types (it only lists
// vite/client), so `process` isn't declared even though vitest genuinely
// runs on Node -- this local, module-scoped ambient declaration covers just
// what this file uses, without reaching for a project-wide config change.
declare const process: { env: Record<string, string | undefined> };

let id = 0;
function makeItem(overrides: Partial<ActionItem> = {}): ActionItem {
  id += 1;
  return {
    id: `action-${id}`,
    thread_id: `thread-${id}`,
    message_id: `msg-${id}`,
    kind: "other",
    title: `Item ${id}`,
    due_at: null,
    due_precision: null,
    due_raw: null,
    amount: null,
    currency: null,
    source_confidence: 0.9,
    status: "open",
    created_at: "2026-01-01T00:00:00.000Z",
    thread_subject: null,
    sender: null,
    provider: "gmail",
    account_email: "operator@gmail.com",
    label: "needs_reply",
    last_message_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

// `process.env.TZ` is read live by Node's Date local getters -- pinning it
// per test is what lets the date-precision cases below assert a specific
// local/UTC mismatch deterministically, regardless of the host running the
// suite.
const ORIGINAL_TZ = process.env.TZ;
afterEach(() => {
  if (ORIGINAL_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = ORIGINAL_TZ;
});

describe("groupActions", () => {
  beforeEach(() => {
    process.env.TZ = "UTC";
  });

  it("always returns all five groups, in display order, even when empty", () => {
    const groups = groupActions([], new Date("2026-08-05T12:00:00.000Z"));
    expect(groups.map((g) => g.key)).toEqual([
      "overdue",
      "today",
      "this_week",
      "later",
      "no_deadline",
    ]);
    expect(groups.every((g) => g.items.length === 0)).toBe(true);
  });

  it("buckets a past datetime deadline as overdue", () => {
    const now = new Date("2026-08-05T12:00:00.000Z");
    const item = makeItem({ due_at: "2026-08-04T09:00:00.000Z", due_precision: "datetime" });
    const groups = groupActions([item], now);
    expect(groups.find((g) => g.key === "overdue")!.items).toEqual([item]);
  });

  it("buckets a same-day datetime deadline as today, even if the time already passed", () => {
    const now = new Date("2026-08-05T18:00:00.000Z");
    const item = makeItem({ due_at: "2026-08-05T09:00:00.000Z", due_precision: "datetime" });
    const groups = groupActions([item], now);
    expect(groups.find((g) => g.key === "today")!.items).toEqual([item]);
  });

  it("buckets days 1-6 out as this week, and day 7+ as later", () => {
    const now = new Date("2026-08-05T00:00:00.000Z");
    const sixDaysOut = makeItem({ due_at: "2026-08-11T00:00:00.000Z", due_precision: "datetime" });
    const sevenDaysOut = makeItem({
      due_at: "2026-08-12T00:00:00.000Z",
      due_precision: "datetime",
    });
    const groups = groupActions([sixDaysOut, sevenDaysOut], now);
    expect(groups.find((g) => g.key === "this_week")!.items).toEqual([sixDaysOut]);
    expect(groups.find((g) => g.key === "later")!.items).toEqual([sevenDaysOut]);
  });

  it("buckets a null due_at as no deadline", () => {
    const item = makeItem({ due_at: null, due_precision: null });
    const groups = groupActions([item], new Date("2026-08-05T00:00:00.000Z"));
    expect(groups.find((g) => g.key === "no_deadline")!.items).toEqual([item]);
  });

  it("compares date-precision deadlines by UTC calendar date, not a naive local conversion", () => {
    // The server pins a date-only deadline at 23:59:59Z of the INTENDED day.
    // In a zone ahead of UTC, converting that instant through local time can
    // read as the next day -- exactly the bug this rule avoids.
    process.env.TZ = "Pacific/Kiritimati"; // UTC+14
    const now = new Date("2026-08-05T08:00:00.000Z"); // local: Aug 5, 22:00
    const item = makeItem({
      due_at: "2026-08-05T23:59:59.000Z", // UTC date: Aug 5; naive local date: Aug 6
      due_precision: "date",
    });
    const groups = groupActions([item], now);
    expect(groups.find((g) => g.key === "today")!.items).toEqual([item]);
    expect(groups.find((g) => g.key === "this_week")!.items).toEqual([]);
  });

  it("still uses local calendar date for a datetime-precision deadline in the same zone", () => {
    // Contrast with the case above: without due_precision="date" pinning,
    // grouping legitimately follows the local wall-clock date.
    process.env.TZ = "Pacific/Kiritimati";
    const now = new Date("2026-08-05T08:00:00.000Z");
    const item = makeItem({
      due_at: "2026-08-05T23:59:59.000Z",
      due_precision: "datetime",
    });
    const groups = groupActions([item], now);
    expect(groups.find((g) => g.key === "this_week")!.items).toEqual([item]);
    expect(groups.find((g) => g.key === "today")!.items).toEqual([]);
  });

  it("keeps two items sharing a thread_id as separate, independently bucketed rows", () => {
    const now = new Date("2026-08-05T00:00:00.000Z");
    const overdueInThread = makeItem({
      thread_id: "shared-thread",
      due_at: "2026-08-01T00:00:00.000Z",
      due_precision: "datetime",
    });
    const laterInThread = makeItem({
      thread_id: "shared-thread",
      due_at: "2026-09-01T00:00:00.000Z",
      due_precision: "datetime",
    });
    const groups = groupActions([overdueInThread, laterInThread], now);
    expect(groups.find((g) => g.key === "overdue")!.items).toEqual([overdueInThread]);
    expect(groups.find((g) => g.key === "later")!.items).toEqual([laterInThread]);
    // Neither collapsed into a single row, and both keep the shared thread_id.
    const allItems = groups.flatMap((g) => g.items);
    expect(allItems).toHaveLength(2);
    expect(new Set(allItems.map((i) => i.thread_id)).size).toBe(1);
    expect(new Set(allItems.map((i) => i.id)).size).toBe(2);
  });

  it("preserves input order within a group", () => {
    const now = new Date("2026-08-05T00:00:00.000Z");
    const a = makeItem({ due_at: "2026-08-10T00:00:00.000Z", due_precision: "datetime" });
    const b = makeItem({ due_at: "2026-08-06T00:00:00.000Z", due_precision: "datetime" });
    const groups = groupActions([a, b], now);
    expect(groups.find((g) => g.key === "this_week")!.items).toEqual([a, b]);
  });
});
