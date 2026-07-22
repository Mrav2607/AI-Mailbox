// Pure debounce/abort orchestration for the live search box — no React, so
// App.tsx's effect wiring is the only place that has to know about hooks.
// One outstanding request at a time: every new issue, whether from the
// debounce timer or an explicit flush, aborts whatever came before it, so a
// slow response for an earlier keystroke can never land after a newer one.
export interface LiveSearchController {
  onInput(q: string): void;
  flush(q: string): void;
  cancel(): void;
}

export function createLiveSearch(opts: {
  debounceMs: number;
  minLength: number;
  run: (q: string, signal: AbortSignal, fromFlush: boolean) => Promise<void>;
  onBelowMin: () => void;
}): LiveSearchController {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;
  // The query the most recently issued request (debounced or flushed) is
  // for — lets a debounce timer skip re-running a search that's already
  // current instead of re-fetching identical results.
  let lastIssued: string | null = null;

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  const issue = (q: string, fromFlush: boolean) => {
    // Abort the previous request before starting the new one, so a stale
    // response can never resolve after a fresher one already landed.
    controller?.abort();
    const ac = new AbortController();
    controller = ac;
    lastIssued = q;
    void opts.run(q, ac.signal, fromFlush);
  };

  // Dropping below minLength abandons search entirely, so any request still
  // in flight has to die here too — otherwise a stale response can resolve
  // after the caller's already switched back to client-side filtering and
  // snap the UI back into search mode out from under it.
  const belowMin = () => {
    controller?.abort();
    lastIssued = null;
    opts.onBelowMin();
  };

  const onInput = (q: string) => {
    const trimmed = q.trim();
    clearTimer();
    if (trimmed.length < opts.minLength) {
      belowMin();
      return;
    }
    if (trimmed === lastIssued) return; // already current — nothing to schedule
    timer = setTimeout(() => {
      timer = null;
      issue(trimmed, false);
    }, opts.debounceMs);
  };

  const flush = (q: string) => {
    const trimmed = q.trim();
    clearTimer();
    if (trimmed.length < opts.minLength) {
      belowMin();
      return;
    }
    issue(trimmed, true);
  };

  const cancel = () => {
    clearTimer();
    controller?.abort();
    // Without this, retyping the same query right after a cancel (bucket
    // switch, clear button) would see it as "already current" and never
    // re-issue — leaving the operator stuck in stale client-filter mode.
    lastIssued = null;
  };

  return { onInput, flush, cancel };
}
