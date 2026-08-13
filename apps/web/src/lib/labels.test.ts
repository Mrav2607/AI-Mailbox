import { describe, expect, it } from "vitest";

import { confidenceColor, confidenceText } from "./labels";

// These thresholds have moved twice, and both times the only thing standing
// between a wrong boundary and production was someone rendering the app and
// looking at it. Pin them.
describe("confidence tiers", () => {
  it("has no color for an unclassified thread", () => {
    expect(confidenceColor(null)).toBe("bg-muted");
    expect(confidenceText(null)).toBe("text-muted-foreground");
  });

  it.each([
    [0, "red"],
    [0.35, "red"],
    [0.36, "amber"],
    [0.8, "amber"],
    [0.81, "green"],
    [1, "green"],
  ])("puts %s in the %s tier", (conf, tier) => {
    expect(confidenceColor(conf)).toBe(`bg-[var(--conf-${tier})]`);
    expect(confidenceText(conf)).toBe(`text-[var(--conf-${tier}-text)]`);
  });

  // The whole reason both helpers round before branching: the bar's color and
  // the percentage printed next to it must never disagree. A raw `c <= 0.35`
  // cut would split this pair even though both render "35%".
  it("tiers on the displayed percentage, not the raw float", () => {
    expect(Math.round(0.3451 * 100)).toBe(35);
    expect(Math.round(0.3549 * 100)).toBe(35);
    expect(confidenceColor(0.3451)).toBe(confidenceColor(0.3549));
    expect(confidenceText(0.3451)).toBe(confidenceText(0.3549));
  });

  // Same contract at the top boundary, where rounding goes the other way.
  it("rounds half up into the next tier", () => {
    expect(Math.round(0.805 * 100)).toBe(81);
    expect(confidenceColor(0.805)).toBe("bg-[var(--conf-green)]");
    expect(confidenceColor(0.804)).toBe("bg-[var(--conf-amber)]");
  });
});
