import { describe, expect, it } from "vitest";

import { backfillToastOutcome, extractionToastOutcome } from "./task-toasts";

describe("backfillToastOutcome", () => {
  it("keeps the site's own clean copy when nothing fell back", () => {
    const outcome = backfillToastOutcome(
      { created: 12, fell_back: 0 },
      "classified 12 · scanned 200",
    );
    expect(outcome).toEqual({ message: "classified 12 · scanned 200", warn: false });
  });

  it("warns and shows the fallback ratio on partial degradation", () => {
    const outcome = backfillToastOutcome(
      { created: 12, fell_back: 38, failure_categories: { http_429: 38 } },
      "classified 12 · scanned 200",
    );
    expect(outcome.warn).toBe(true);
    expect(outcome.message).toBe(
      "classified 12 · 38 fell back to the built-in model — rate limited by your provider",
    );
  });

  it("warns on total failure the same way as partial degradation", () => {
    const outcome = backfillToastOutcome(
      { created: 0, fell_back: 50, failure_categories: { timed_out: 50 } },
      "classified 0 · scanned 50",
    );
    expect(outcome).toEqual({
      message: "classified 0 · 50 fell back to the built-in model — provider timed out",
      warn: true,
    });
  });

  it("skips the plain-words suffix when no category dominates", () => {
    const outcome = backfillToastOutcome(
      { created: 5, fell_back: 10, failure_categories: { http_429: 5, timed_out: 5 } },
      "classified 5 · scanned 15",
    );
    expect(outcome).toEqual({
      message: "classified 5 · 10 fell back to the built-in model",
      warn: true,
    });
  });

  it("treats a result missing every new field as a clean run", () => {
    // What an older API/worker result recorded before this feature shipped
    // looks like -- must not throw, must not warn.
    const outcome = backfillToastOutcome({}, "classified 12 · scanned 200");
    expect(outcome).toEqual({ message: "classified 12 · scanned 200", warn: false });
  });
});

describe("extractionToastOutcome", () => {
  it("reports extracted count on a clean run", () => {
    const outcome = extractionToastOutcome({ extracted: 5, failed: 0 });
    expect(outcome).toEqual({
      message: "action extraction complete · 5 extracted",
      warn: false,
    });
  });

  it("warns and shows the extracted/failed ratio on partial degradation", () => {
    const outcome = extractionToastOutcome({
      extracted: 3,
      failed: 2,
      failure_categories: { connection_failed: 2 },
    });
    expect(outcome.warn).toBe(true);
    expect(outcome.message).toBe(
      "action extraction · 3 extracted · 2 failed — could not reach your provider",
    );
  });

  it("warns on total failure -- nothing extracted", () => {
    const outcome = extractionToastOutcome({
      extracted: 0,
      failed: 8,
      failure_categories: { http_429: 8 },
    });
    expect(outcome).toEqual({
      message: "action extraction · 0 extracted · 8 failed — rate limited by your provider",
      warn: true,
    });
  });

  it("does not throw and still produces a sensible toast when fields are missing", () => {
    const outcome = extractionToastOutcome({});
    expect(outcome).toEqual({
      message: "action extraction complete · 0 extracted",
      warn: false,
    });
  });

  it("leaves an unmapped failure category out of the message", () => {
    const outcome = extractionToastOutcome({
      extracted: 1,
      failed: 4,
      failure_categories: { some_new_category_the_ui_has_never_seen: 4 },
    });
    expect(outcome).toEqual({
      message: "action extraction · 1 extracted · 4 failed",
      warn: true,
    });
  });
});
