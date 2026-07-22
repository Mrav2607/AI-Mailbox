import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createLiveSearch, type LiveSearchController } from "./live-search";

describe("createLiveSearch", () => {
  let controller: LiveSearchController;
  let run: ReturnType<typeof vi.fn<(q: string, signal: AbortSignal, fromFlush: boolean) => Promise<void>>>;
  let onBelowMin: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    vi.useFakeTimers();
    run = vi.fn().mockResolvedValue(undefined);
    onBelowMin = vi.fn();
    controller = createLiveSearch({ debounceMs: 300, minLength: 2, run, onBelowMin });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not call run before the debounce elapses", () => {
    controller.onInput("hello");
    vi.advanceTimersByTime(299);
    expect(run).not.toHaveBeenCalled();
  });

  it("calls run once the debounce elapses", () => {
    controller.onInput("hello");
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith("hello", expect.any(AbortSignal), false);
  });

  it("collapses rapid retypes into a single call for the last query", () => {
    controller.onInput("h");
    vi.advanceTimersByTime(100);
    controller.onInput("he");
    vi.advanceTimersByTime(100);
    controller.onInput("hel");
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith("hel", expect.any(AbortSignal), false);
  });

  it("never fires below minLength and calls onBelowMin instead", () => {
    controller.onInput("h");
    vi.advanceTimersByTime(1000);
    expect(run).not.toHaveBeenCalled();
    expect(onBelowMin).toHaveBeenCalledTimes(1);
  });

  it("flush fires immediately, cancels any pending timer, and aborts an in-flight request", () => {
    const signals: AbortSignal[] = [];
    run.mockImplementation((_q: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<void>(() => {}); // never resolves — simulates in-flight
    });

    controller.onInput("first");
    vi.advanceTimersByTime(300); // issues "first", now in-flight
    expect(run).toHaveBeenCalledTimes(1);

    controller.onInput("second"); // schedules a debounce timer
    controller.flush("third");
    expect(run).toHaveBeenCalledTimes(2);
    expect(run).toHaveBeenLastCalledWith("third", expect.any(AbortSignal), true);
    expect(signals[0].aborted).toBe(true); // "first"'s request was aborted

    // the pending "second" debounce never fires
    vi.advanceTimersByTime(1000);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("skips a debounce timer for a query identical to the last flushed query", () => {
    controller.flush("same");
    expect(run).toHaveBeenCalledTimes(1);
    controller.onInput("same");
    vi.advanceTimersByTime(1000);
    expect(run).toHaveBeenCalledTimes(1); // no new call — identical to what's already current
  });

  it("cancel clears the pending timer and aborts an in-flight request", () => {
    const signals: AbortSignal[] = [];
    run.mockImplementation((_q: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<void>(() => {});
    });

    controller.onInput("first");
    vi.advanceTimersByTime(300);
    controller.onInput("second");
    controller.cancel();
    expect(signals[0].aborted).toBe(true);

    vi.advanceTimersByTime(1000);
    expect(run).toHaveBeenCalledTimes(1); // "second"'s timer never fired
  });

  it("aborts an in-flight request when input drops below minLength", () => {
    const signals: AbortSignal[] = [];
    run.mockImplementation((_q: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<void>(() => {}); // never resolves — simulates in-flight
    });

    controller.onInput("abc");
    vi.advanceTimersByTime(300); // issues "abc", now in-flight
    expect(run).toHaveBeenCalledTimes(1);

    controller.onInput("a"); // dips below minLength (2)
    expect(onBelowMin).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(true);
  });

  it("re-fires the same query after cancel() resets what counts as current", () => {
    controller.onInput("same");
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(1);

    controller.cancel();
    controller.onInput("same");
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("re-fires the same query after a below-min dip in between", () => {
    controller.onInput("hello");
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(1);

    controller.onInput("h"); // dips below minLength — should reset lastIssued
    expect(onBelowMin).toHaveBeenCalledTimes(1);

    controller.onInput("hello"); // same query as before the dip
    vi.advanceTimersByTime(300);
    expect(run).toHaveBeenCalledTimes(2);
  });
});
