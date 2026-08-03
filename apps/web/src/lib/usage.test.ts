import { describe, expect, it } from "vitest";

import { chartMax, fillDailySeries, summarizeUsage } from "./usage";
import type { LlmUsage, LlmUsageDailyPoint } from "./types";

const EMPTY_COUNTERS = {
  calls: 0,
  calls_with_total_tokens: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  total_tokens: 0,
};

describe("fillDailySeries", () => {
  it("zero-fills the gaps in a sparse window", () => {
    // "today" is 2026-08-03 UTC; the API only sent rows for the 1st and 3rd.
    const today = new Date("2026-08-03T12:00:00Z");
    const daily: LlmUsageDailyPoint[] = [
      { date: "2026-08-01", calls: 5, total_tokens: 500 },
      { date: "2026-08-03", calls: 2, total_tokens: 200 },
    ];
    const filled = fillDailySeries(daily, 3, today);
    expect(filled.map((d) => d.date)).toEqual([
      "2026-08-01",
      "2026-08-02",
      "2026-08-03",
    ]);
    expect(filled[0]).toEqual({ date: "2026-08-01", calls: 5, total_tokens: 500 });
    expect(filled[1]).toEqual({ date: "2026-08-02", calls: 0, total_tokens: 0 });
    expect(filled[2]).toEqual({ date: "2026-08-03", calls: 2, total_tokens: 200 });
  });

  it("never draws scattered days as an adjacent block", () => {
    // Three real days of usage, a week apart -- if the gaps weren't filled,
    // these would render as three neighboring bars instead of a mostly-quiet
    // month with three spikes.
    const today = new Date("2026-08-14T00:00:00Z");
    const daily: LlmUsageDailyPoint[] = [
      { date: "2026-08-01", calls: 3, total_tokens: 30 },
      { date: "2026-08-08", calls: 4, total_tokens: 40 },
      { date: "2026-08-14", calls: 1, total_tokens: 10 },
    ];
    const filled = fillDailySeries(daily, 14, today);
    expect(filled).toHaveLength(14);
    const nonZeroDates = filled.filter((d) => d.calls > 0).map((d) => d.date);
    expect(nonZeroDates).toEqual(["2026-08-01", "2026-08-08", "2026-08-14"]);
    const zeroRun = filled.filter((d) => d.date > "2026-08-01" && d.date < "2026-08-08");
    expect(zeroRun.every((d) => d.calls === 0)).toBe(true);
  });

  it("handles an entirely empty input", () => {
    const today = new Date("2026-08-03T00:00:00Z");
    const filled = fillDailySeries([], 5, today);
    expect(filled).toHaveLength(5);
    expect(filled.every((d) => d.calls === 0 && d.total_tokens === 0)).toBe(true);
    expect(filled[filled.length - 1].date).toBe("2026-08-03");
  });

  it("returns exactly one entry for a 1-day window", () => {
    const today = new Date("2026-08-03T23:59:00Z");
    const daily: LlmUsageDailyPoint[] = [
      { date: "2026-08-03", calls: 7, total_tokens: 700 },
    ];
    const filled = fillDailySeries(daily, 1, today);
    expect(filled).toEqual([{ date: "2026-08-03", calls: 7, total_tokens: 700 }]);
  });

  it("stays on the right UTC calendar day regardless of local offset", () => {
    // 2026-08-03T23:30 UTC is already 2026-08-04 in a UTC+1 zone -- the
    // window boundary must be computed off the UTC date, not whatever the
    // local Date getters would say.
    const today = new Date("2026-08-03T23:30:00Z");
    const filled = fillDailySeries([], 2, today);
    expect(filled.map((d) => d.date)).toEqual(["2026-08-02", "2026-08-03"]);
  });
});

describe("summarizeUsage", () => {
  it("computes active days, busiest day, average, and token coverage", () => {
    const usage: LlmUsage = {
      window_days: 3,
      totals: {
        calls: 10,
        calls_with_total_tokens: 8,
        prompt_tokens: 900,
        completion_tokens: 100,
        total_tokens: 1000,
      },
      by_stage: [],
      by_provider: [],
      daily: [
        { date: "2026-08-01", calls: 3, total_tokens: 300 },
        { date: "2026-08-02", calls: 7, total_tokens: 700 },
      ],
    };
    const summary = summarizeUsage(usage);
    expect(summary.activeDays).toBe(2);
    expect(summary.busiestDay).toEqual({ date: "2026-08-02", calls: 7, total_tokens: 700 });
    expect(summary.avgCallsPerActiveDay).toBe(5);
    expect(summary.tokenCoverage).toBe(0.8);
  });

  it("never produces NaN for a zero-call window", () => {
    const usage: LlmUsage = {
      window_days: 7,
      totals: { ...EMPTY_COUNTERS },
      by_stage: [],
      by_provider: [],
      daily: [],
    };
    const summary = summarizeUsage(usage);
    expect(summary).toEqual({
      activeDays: 0,
      busiestDay: null,
      avgCallsPerActiveDay: 0,
      tokenCoverage: 0,
    });
  });
});

describe("chartMax", () => {
  it("rounds up to a nice axis ceiling instead of an ugly raw max", () => {
    expect(chartMax([37])).toBe(50);
    expect(chartMax([3, 12, 37])).toBe(50);
    expect(chartMax([100])).toBe(100);
    expect(chartMax([1])).toBe(1);
  });

  it("returns a sane positive ceiling for an all-zero series", () => {
    expect(chartMax([0, 0, 0])).toBe(1);
    expect(chartMax([])).toBe(1);
  });
});
