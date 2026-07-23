import { describe, expect, it } from "vitest";

import { dateGroup } from "./time";

// Wednesday, 2026-07-22, 14:30 local time — mid-afternoon so "today" isn't
// accidentally satisfied by every test regardless of clock.
const NOW = new Date(2026, 6, 22, 14, 30, 0);

function atLocal(y: number, m: number, d: number, h = 12): string {
  return new Date(y, m, d, h).toISOString();
}

describe("dateGroup", () => {
  it("groups a timestamp later today as today", () => {
    expect(dateGroup(atLocal(2026, 6, 22, 23), NOW)).toBe("today");
  });

  it("groups exactly the start of today as today", () => {
    expect(dateGroup(new Date(2026, 6, 22, 0, 0, 0).toISOString(), NOW)).toBe(
      "today",
    );
  });

  it("groups a future timestamp as today", () => {
    expect(dateGroup(atLocal(2026, 7, 1), NOW)).toBe("today");
  });

  it("groups one millisecond before today's start as yesterday", () => {
    const edge = new Date(2026, 6, 22, 0, 0, 0, 0).getTime() - 1;
    expect(dateGroup(new Date(edge).toISOString(), NOW)).toBe("yesterday");
  });

  it("groups exactly 1 day ago as yesterday", () => {
    expect(dateGroup(new Date(2026, 6, 21, 0, 0, 0).toISOString(), NOW)).toBe(
      "yesterday",
    );
  });

  it("groups one millisecond before the yesterday floor as this week", () => {
    const edge = new Date(2026, 6, 21, 0, 0, 0, 0).getTime() - 1;
    expect(dateGroup(new Date(edge).toISOString(), NOW)).toBe("this week");
  });

  it("groups exactly 6 days ago as this week", () => {
    expect(dateGroup(new Date(2026, 6, 16, 0, 0, 0).toISOString(), NOW)).toBe(
      "this week",
    );
  });

  it("groups one millisecond before the week floor as this month", () => {
    const edge = new Date(2026, 6, 16, 0, 0, 0, 0).getTime() - 1;
    expect(dateGroup(new Date(edge).toISOString(), NOW)).toBe("this month");
  });

  it("groups exactly 29 days ago as this month", () => {
    expect(dateGroup(new Date(2026, 5, 23, 0, 0, 0).toISOString(), NOW)).toBe(
      "this month",
    );
  });

  it("groups one millisecond before the month floor as older", () => {
    const edge = new Date(2026, 5, 23, 0, 0, 0, 0).getTime() - 1;
    expect(dateGroup(new Date(edge).toISOString(), NOW)).toBe("older");
  });

  it("groups a far past timestamp as older", () => {
    expect(dateGroup(atLocal(2020, 0, 1), NOW)).toBe("older");
  });

  it("treats null as older", () => {
    expect(dateGroup(null, NOW)).toBe("older");
  });

  it("treats an unparseable string as older", () => {
    expect(dateGroup("not a date", NOW)).toBe("older");
  });

  it("defaults now to the current time when omitted", () => {
    expect(dateGroup(new Date().toISOString())).toBe("today");
  });
});
