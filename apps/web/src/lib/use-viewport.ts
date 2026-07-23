import { useSyncExternalStore } from "react";

// Narrow is defined as the exact complement of Tailwind's `md` breakpoint —
// not its own max-width query — so a target that's only rendered `md:block`
// never resolves while the tour thinks the viewport is wide enough for it.
const WIDE_QUERY = "(min-width: 768px)";

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(WIDE_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  return !window.matchMedia(WIDE_QUERY).matches;
}

export function useIsNarrow(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
