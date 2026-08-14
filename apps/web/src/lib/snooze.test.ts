import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  formatWakeTime,
  laterToday,
  nextWeek,
  thisWeekend,
  tomorrowMorning,
} from "./snooze";

// Same rationale as agenda.test.ts: process.env.TZ is read live by Node's
// Date local getters, so pinning it per test is what lets the weekday/DST
// cases below assert deterministically regardless of the host running the
// suite.
declare const process: { env: Record<string, string | undefined> };
const ORIGINAL_TZ = process.env.TZ;
afterEach(() => {
  if (ORIGINAL_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = ORIGINAL_TZ;
});
beforeEach(() => {
  process.env.TZ = "UTC";
});

describe("laterToday", () => {
  it("is exactly +3h from now", () => {
    const now = new Date("2026-08-14T10:00:00");
    expect(laterToday(now).getTime()).toBe(now.getTime() + 3 * 60 * 60 * 1000);
  });

  it("is a literal duration add, not wall-clock-preserving, across a DST fallback", () => {
    // America/New_York fall-back: 2027-11-07 02:00 -> 01:00 (clocks repeat
    // the 1-2am hour). +3h of wall clock from 00:30 would land at 03:30, but
    // this preset adds a literal 3h duration -- the "later today" contract
    // is a fixed offset from the click, not a fixed wall-clock target.
    process.env.TZ = "America/New_York";
    const now = new Date("2027-11-07T00:30:00");
    expect(laterToday(now).getTime()).toBe(now.getTime() + 3 * 60 * 60 * 1000);
  });
});

describe("tomorrowMorning", () => {
  it("is tomorrow's date at 08:00, from an early-morning click", () => {
    const now = new Date("2026-08-14T06:00:00"); // Friday
    const d = tomorrowMorning(now);
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(7); // August, 0-indexed
    expect(d.getDate()).toBe(15);
    expect(d.getHours()).toBe(8);
    expect(d.getMinutes()).toBe(0);
  });

  it("is tomorrow's date at 08:00 even from a late-evening click", () => {
    const now = new Date("2026-08-14T23:59:00");
    const d = tomorrowMorning(now);
    expect(d.getDate()).toBe(15);
    expect(d.getHours()).toBe(8);
  });

  it("is always in the future relative to now", () => {
    const now = new Date("2026-08-14T07:59:00");
    expect(tomorrowMorning(now).getTime()).toBeGreaterThan(now.getTime());
  });

  it("stays at wall-clock 08:00 across a DST spring-forward", () => {
    // America/New_York spring-forward: 2027-03-14 02:00 -> 03:00. A naive
    // now-plus-24h-in-ms would land at 07:00 or 09:00 depending on which
    // side of the gap it started on -- component construction always means
    // "08:00 local on this date", DST notwithstanding.
    process.env.TZ = "America/New_York";
    const now = new Date("2027-03-13T20:00:00");
    const d = tomorrowMorning(now);
    expect(d.getDate()).toBe(14);
    expect(d.getHours()).toBe(8);
  });
});

describe("thisWeekend", () => {
  it("resolves to the coming Saturday from a Friday", () => {
    const now = new Date("2026-08-14T10:00:00"); // Friday
    const d = thisWeekend(now);
    expect(d.getDay()).toBe(6);
    expect(d.getDate()).toBe(15);
    expect(d.getHours()).toBe(9);
  });

  it("resolves to later today from a Saturday morning, before the anchor time", () => {
    const now = new Date("2026-08-15T07:00:00"); // Saturday, before 09:00
    const d = thisWeekend(now);
    expect(d.getDate()).toBe(15);
    expect(d.getHours()).toBe(9);
    expect(d.getTime()).toBeGreaterThan(now.getTime());
  });

  it("rolls to next Saturday from a Saturday afternoon, after the anchor time", () => {
    const now = new Date("2026-08-15T14:00:00"); // Saturday, after 09:00
    const d = thisWeekend(now);
    expect(d.getDate()).toBe(22);
    expect(d.getDay()).toBe(6);
    expect(d.getTime()).toBeGreaterThan(now.getTime());
  });

  it("resolves to the coming Saturday (6 days out) from a Sunday", () => {
    const now = new Date("2026-08-16T10:00:00"); // Sunday
    const d = thisWeekend(now);
    expect(d.getDate()).toBe(22);
    expect(d.getDay()).toBe(6);
  });

  it("never lands in the past", () => {
    for (let day = 0; day < 7; day++) {
      const now = new Date(2026, 7, 9 + day, 23, 30); // a week spanning every weekday
      expect(thisWeekend(now).getTime()).toBeGreaterThan(now.getTime());
    }
  });
});

describe("nextWeek", () => {
  it("resolves to the coming Monday from a Friday", () => {
    const now = new Date("2026-08-14T10:00:00"); // Friday
    const d = nextWeek(now);
    expect(d.getDay()).toBe(1);
    expect(d.getDate()).toBe(17);
    expect(d.getHours()).toBe(8);
  });

  it("always leaves the current week, even from a Monday morning", () => {
    const now = new Date("2026-08-17T06:00:00"); // Monday, before 08:00
    const d = nextWeek(now);
    expect(d.getDay()).toBe(1);
    expect(d.getDate()).toBe(24);
  });

  it("never lands in the past", () => {
    for (let day = 0; day < 7; day++) {
      const now = new Date(2026, 7, 9 + day, 23, 30);
      expect(nextWeek(now).getTime()).toBeGreaterThan(now.getTime());
    }
  });
});

describe("formatWakeTime", () => {
  it("returns an em dash for a null timestamp", () => {
    expect(formatWakeTime(null)).toBe("—");
  });

  it("returns an em dash for an unparseable timestamp", () => {
    expect(formatWakeTime("not-a-date")).toBe("—");
  });

  it("labels a same-day wake as today", () => {
    const now = new Date("2026-08-14T06:00:00");
    expect(formatWakeTime("2026-08-14T15:00:00", now)).toBe("wakes today 15:00");
  });

  it("labels a next-day wake as tomorrow", () => {
    const now = new Date("2026-08-14T06:00:00");
    expect(formatWakeTime("2026-08-15T08:00:00", now)).toBe("wakes tomorrow 08:00");
  });

  it("labels a wake 2-6 days out by weekday name", () => {
    const now = new Date("2026-08-14T06:00:00"); // Friday
    expect(formatWakeTime("2026-08-17T08:00:00", now)).toBe("wakes Monday 08:00");
  });

  it("labels a wake 7+ days out by full date", () => {
    const now = new Date("2026-08-14T06:00:00");
    const d = new Date("2026-08-22T09:00:00");
    expect(formatWakeTime(d.toISOString(), now)).toBe(
      `wakes ${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`,
    );
  });
});
