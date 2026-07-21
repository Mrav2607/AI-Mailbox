import { describe, expect, it } from "vitest";

import {
  allRunsDeduplicated,
  sumIngestResults,
  waitForSyncRuns,
  type SyncRunStatus,
} from "./api";

function run(patch: Partial<SyncRunStatus>): SyncRunStatus {
  return {
    run_id: "run-1",
    mode: "manual",
    status: "succeeded",
    ready: true,
    deduplicated: false,
    provider_account_id: null,
    result: null,
    ...patch,
  };
}

describe("allRunsDeduplicated", () => {
  it("is false for an empty batch (nothing connected, not 'already running')", () => {
    expect(allRunsDeduplicated([])).toBe(false);
  });

  it("is false when only some accounts' runs deduplicated", () => {
    const runs = [run({ deduplicated: true }), run({ deduplicated: false })];
    expect(allRunsDeduplicated(runs)).toBe(false);
  });

  it("is true only once every account's run deduplicated", () => {
    const runs = [
      run({ run_id: "a", deduplicated: true }),
      run({ run_id: "b", deduplicated: true }),
    ];
    expect(allRunsDeduplicated(runs)).toBe(true);
  });
});

describe("sumIngestResults", () => {
  it("sums threads and messages across every account's run", () => {
    const finals = [
      run({ result: { status: "ok", threads_upserted: 3, messages_upserted: 9 } }),
      run({ result: { status: "ok", threads_upserted: 1, messages_upserted: 2 } }),
    ];
    expect(sumIngestResults(finals)).toEqual({ threads: 4, messages: 11 });
  });

  it("falls back to new_threads when threads_upserted is missing", () => {
    const finals = [run({ result: { status: "ok", new_threads: 2 } })];
    expect(sumIngestResults(finals)).toEqual({ threads: 2, messages: 0 });
  });

  it("treats a run with no result as contributing nothing", () => {
    expect(sumIngestResults([run({ result: null })])).toEqual({ threads: 0, messages: 0 });
  });
});

describe("waitForSyncRuns", () => {
  it("resolves already-ready runs immediately, without polling", async () => {
    const runs = [run({ run_id: "a" }), run({ run_id: "b" })];
    const settled = await waitForSyncRuns(runs);
    expect(settled).toHaveLength(2);
    expect(settled.every((s) => s.status === "fulfilled")).toBe(true);
    const values = settled.map((s) => (s as PromiseFulfilledResult<SyncRunStatus>).value);
    expect(values.map((v) => v.run_id)).toEqual(["a", "b"]);
  });
});
