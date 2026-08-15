import { describe, expect, it } from "vitest";

import { backfillToastOutcome, extractionToastOutcome, ingestToastOutcome } from "./task-toasts";

describe("backfillToastOutcome", () => {
  it("keeps the site's own clean copy when nothing fell back", () => {
    const outcome = backfillToastOutcome(
      { created: 12, fell_back: 0 },
      "classified 12 · scanned 200",
    );
    expect(outcome).toEqual({ message: "classified 12 · scanned 200", level: "success" });
  });

  it("warns and shows the fallback ratio on partial degradation", () => {
    const outcome = backfillToastOutcome(
      { created: 12, fell_back: 38, failure_categories: { http_429: 38 } },
      "classified 12 · scanned 200",
    );
    expect(outcome.level).toBe("warning");
    expect(outcome.message).toBe(
      "classified 12 · 38 of those fell back to the built-in model — rate limited by your provider",
    );
  });

  it("warns on total failure the same way as partial degradation", () => {
    const outcome = backfillToastOutcome(
      { created: 0, fell_back: 50, failure_categories: { timed_out: 50 } },
      "classified 0 · scanned 50",
    );
    expect(outcome).toEqual({
      message: "classified 0 · 50 of those fell back to the built-in model — provider timed out",
      level: "warning",
    });
  });

  it("skips the plain-words suffix when no category dominates", () => {
    const outcome = backfillToastOutcome(
      { created: 5, fell_back: 10, failure_categories: { http_429: 5, timed_out: 5 } },
      "classified 5 · scanned 15",
    );
    expect(outcome).toEqual({
      message: "classified 5 · 10 of those fell back to the built-in model",
      level: "warning",
    });
  });

  it("treats a result missing every new field as a clean run", () => {
    // What an older API/worker result recorded before this feature shipped
    // looks like -- must not throw, must not warn.
    const outcome = backfillToastOutcome({}, "classified 12 · scanned 200");
    expect(outcome).toEqual({ message: "classified 12 · scanned 200", level: "success" });
  });

  it("errors, names the category, and states the remedy when the run stopped for lack of a fallback", () => {
    // Phase 2 (docs/plans/2026-08-14-llm-failure-visibility-plan.md): the
    // user isn't opted into the local-model fallback, so the run stopped the
    // moment the LLM failed instead of burning more of their own quota.
    const outcome = backfillToastOutcome(
      {
        status: "llm_unavailable",
        created: 7,
        failure_categories: { http_429: 9 },
      },
      "classified 7 · scanned 50",
    );
    expect(outcome.level).toBe("error");
    expect(outcome.message).toContain("7 classified so far");
    expect(outcome.message).toContain("rate limited by your provider");
    expect(outcome.message).toContain("run backfill again once your provider recovers");
  });

  it("still errors and states the remedy when llm_unavailable carries no dominant category", () => {
    const outcome = backfillToastOutcome(
      { status: "llm_unavailable", created: 0 },
      "classified 0 · scanned 50",
    );
    expect(outcome.level).toBe("error");
    expect(outcome.message).toContain("0 classified so far");
    expect(outcome.message).toContain("run backfill again once your provider recovers");
  });
});

describe("extractionToastOutcome", () => {
  it("reports extracted count on a clean run", () => {
    const outcome = extractionToastOutcome({ extracted: 5, failed: 0 });
    expect(outcome).toEqual({
      message: "action extraction complete · 5 extracted",
      level: "success",
    });
  });

  it("errors and names the remedy when the sweep gave up partway", () => {
    // Phase 3: the sweep stops after a losing streak, so the candidates it
    // never reached are invisible in the counts -- the toast has to say it
    // stopped rather than imply the numbers are the whole story.
    const outcome = extractionToastOutcome({
      status: "llm_unavailable",
      extracted: 2,
      failed: 3,
      failure_categories: { http_429: 3 },
    });
    expect(outcome.level).toBe("error");
    expect(outcome.message).toBe(
      "action extraction stopped early · 2 extracted so far — rate limited by your provider" +
        " — run it again once your provider recovers",
    );
  });

  it("treats a missing status as a normal run", () => {
    // An older API/worker result predates the field; it must not read as a
    // stopped-early run.
    expect(extractionToastOutcome({ extracted: 4, failed: 0 }).level).toBe("success");
  });

  it("warns and shows the extracted/failed ratio on partial degradation", () => {
    const outcome = extractionToastOutcome({
      extracted: 3,
      failed: 2,
      failure_categories: { connection_failed: 2 },
    });
    expect(outcome.level).toBe("warning");
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
      level: "warning",
    });
  });

  it("does not throw and still produces a sensible toast when fields are missing", () => {
    const outcome = extractionToastOutcome({});
    expect(outcome).toEqual({
      message: "action extraction complete · 0 extracted",
      level: "success",
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
      level: "warning",
    });
  });
});

describe("ingestToastOutcome", () => {
  it("keeps the site's own clean copy when nothing was left unclassified", () => {
    const outcome = ingestToastOutcome({ left_unclassified: 0 }, "ingest complete · 3 threads");
    expect(outcome).toEqual({ message: "ingest complete · 3 threads", level: "success" });
  });

  it("warns and names the remedy when mail was left unclassified", () => {
    const outcome = ingestToastOutcome({ left_unclassified: 4 }, "ingest complete · 3 threads");
    expect(outcome).toEqual({
      message:
        "ingest complete · 3 threads · 4 left unclassified — run backfill when your provider recovers",
      level: "warning",
    });
  });

  it("treats a result missing left_unclassified as a clean run", () => {
    // An older API (or an account not on BYOK classification) never sends
    // this field at all -- must not throw, must not warn.
    const outcome = ingestToastOutcome({}, "ingest complete · 3 threads");
    expect(outcome).toEqual({ message: "ingest complete · 3 threads", level: "success" });
  });
});
