import { beforeEach, describe, expect, it, vi } from "vitest";

import { SEEN_CAP, THREAD_SEEN_KEY_PREFIX, isUnseen, loadSeen, markSeen } from "./seen";

const USER = "user-1";

describe("seen store", () => {
  beforeEach(() => window.localStorage.clear());

  it("uses the exact per-user key prefix, not the auto-sync watermark key", () => {
    const map = loadSeen(USER);
    markSeen(map, USER, "t1", "2026-07-01T00:00:00Z");

    expect(window.localStorage.getItem(`${THREAD_SEEN_KEY_PREFIX}${USER}`)).not.toBeNull();
    expect(THREAD_SEEN_KEY_PREFIX).toBe("ai_mailbox_thread_seen:");
    expect(THREAD_SEEN_KEY_PREFIX).not.toBe("ai_mailbox_seen:");
  });

  it("round-trips entries through persist and reload", () => {
    const map = loadSeen(USER);
    markSeen(map, USER, "t1", "2026-07-01T00:00:00Z");
    markSeen(map, USER, "t2", null);

    const reloaded = loadSeen(USER);
    expect(reloaded.get("t1")).toBe("2026-07-01T00:00:00Z");
    expect(reloaded.get("t2")).toBe("");
  });

  it("keeps separate stores per user", () => {
    const mapA = loadSeen("a");
    markSeen(mapA, "a", "t1", "2026-07-01T00:00:00Z");

    expect(loadSeen("b").size).toBe(0);
  });

  it("re-marking touches LRU order to the end", () => {
    const map = loadSeen(USER);
    markSeen(map, USER, "t1", "2026-07-01T00:00:00Z");
    markSeen(map, USER, "t2", "2026-07-01T00:00:00Z");
    markSeen(map, USER, "t3", "2026-07-01T00:00:00Z");
    // Touch t1 again — it should move behind t2/t3 in insertion order.
    markSeen(map, USER, "t1", "2026-07-02T00:00:00Z");

    expect(Array.from(map.keys())).toEqual(["t2", "t3", "t1"]);
  });

  it("prunes the oldest-touched entries once past the cap", () => {
    const map = new Map<string, string>();
    for (let i = 0; i < SEEN_CAP + 5; i++) {
      markSeen(map, USER, `t${i}`, "2026-07-01T00:00:00Z");
    }

    expect(map.size).toBe(SEEN_CAP);
    // The first 5 inserted (oldest-touched) should be evicted.
    expect(map.has("t0")).toBe(false);
    expect(map.has("t4")).toBe(false);
    expect(map.has("t5")).toBe(true);
    expect(map.has(`t${SEEN_CAP + 4}`)).toBe(true);
  });

  it("isUnseen: no entry is always unseen", () => {
    const map = new Map<string, string>();
    expect(isUnseen(map, "t1", "2026-07-01T00:00:00Z")).toBe(true);
    expect(isUnseen(map, "t1", null)).toBe(true);
  });

  it("isUnseen: a null lastMessageAt with an existing entry is seen", () => {
    const map = new Map<string, string>([["t1", "2026-07-01T00:00:00Z"]]);
    expect(isUnseen(map, "t1", null)).toBe(false);
  });

  it("isUnseen: newer activity re-bolds a previously seen thread", () => {
    const map = new Map<string, string>([["t1", "2026-07-01T00:00:00Z"]]);
    expect(isUnseen(map, "t1", "2026-07-02T00:00:00Z")).toBe(true);
    expect(isUnseen(map, "t1", "2026-07-01T00:00:00Z")).toBe(false);
    expect(isUnseen(map, "t1", "2026-06-01T00:00:00Z")).toBe(false);
  });

  it("isUnseen: a null-timestamp entry ('') compares older than any ISO string", () => {
    const map = new Map<string, string>([["t1", ""]]);
    expect(isUnseen(map, "t1", "2026-07-01T00:00:00Z")).toBe(true);
    expect(isUnseen(map, "t1", null)).toBe(false);
  });

  it("loadSeen returns an empty map for missing storage", () => {
    expect(loadSeen(USER).size).toBe(0);
  });

  it("loadSeen returns an empty map for corrupt JSON", () => {
    window.localStorage.setItem(`${THREAD_SEEN_KEY_PREFIX}${USER}`, "{not json");
    expect(loadSeen(USER).size).toBe(0);
  });

  it("loadSeen skips malformed entries but keeps valid ones", () => {
    window.localStorage.setItem(
      `${THREAD_SEEN_KEY_PREFIX}${USER}`,
      JSON.stringify([
        ["t1", "2026-07-01T00:00:00Z"],
        ["t2"],
        [123, "2026-07-01T00:00:00Z"],
        "not-a-pair",
        ["t3", 42],
      ]),
    );
    const map = loadSeen(USER);
    expect(map.size).toBe(1);
    expect(map.get("t1")).toBe("2026-07-01T00:00:00Z");
  });

  it("loadSeen returns an empty map when the stored value isn't an array", () => {
    window.localStorage.setItem(
      `${THREAD_SEEN_KEY_PREFIX}${USER}`,
      JSON.stringify({ t1: "2026-07-01T00:00:00Z" }),
    );
    expect(loadSeen(USER).size).toBe(0);
  });

  it("markSeen swallows a storage.setItem failure without throwing", () => {
    const spy = vi
      .spyOn(window.localStorage.__proto__, "setItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded");
      });

    const map = loadSeen(USER);
    expect(() => markSeen(map, USER, "t1", "2026-07-01T00:00:00Z")).not.toThrow();
    // The in-memory map is still updated even though persistence failed.
    expect(map.get("t1")).toBe("2026-07-01T00:00:00Z");

    spy.mockRestore();
  });
});
