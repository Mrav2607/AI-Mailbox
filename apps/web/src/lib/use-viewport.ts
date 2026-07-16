import { useSyncExternalStore } from "react";

const NARROW_QUERY = "(max-width: 767px)";

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(NARROW_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  return window.matchMedia(NARROW_QUERY).matches;
}

export function useIsNarrow(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
