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
    // Both sit in the red tier despite straddling 0.35, because both print
    // "35%". Asserted absolutely, not just as equal to each other -- two
    // identically wrong results would satisfy equality alone.
    expect(confidenceColor(0.3451)).toBe("bg-[var(--conf-red)]");
    expect(confidenceColor(0.3549)).toBe("bg-[var(--conf-red)]");
    expect(confidenceText(0.3451)).toBe("text-[var(--conf-red-text)]");
    expect(confidenceText(0.3549)).toBe("text-[var(--conf-red-text)]");
  });

  // Every rounding boundary, on BOTH helpers. Pinning only the color would let
  // a change to confidenceText alone (say, Math.floor) ship a row whose bar and
  // printed number disagree -- the exact bug the rounding contract prevents.
  it.each([
    [0.3549, "red"],
    [0.355, "amber"],
    [0.8049, "amber"],
    [0.805, "green"],
  ])("rounds %s into the %s tier in both helpers", (conf, tier) => {
    expect(confidenceColor(conf)).toBe(`bg-[var(--conf-${tier})]`);
    expect(confidenceText(conf)).toBe(`text-[var(--conf-${tier}-text)]`);
  });
});
