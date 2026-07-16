import { useEffect } from "react";

type Handler = (e: KeyboardEvent) => void;

export function shouldSuppressConsoleHotkeys(
  paletteOpen: boolean,
  shortcutsOpen: boolean,
  tourActive: boolean,
): boolean {
  return paletteOpen || shortcutsOpen || tourActive;
}

export function useHotkeys(handler: Handler, deps: unknown[] = []) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      const isTyping =
        tag === "INPUT" || tag === "TEXTAREA" || t?.isContentEditable;
      // Always allow Cmd/Ctrl-K and Escape even while typing
      const isPalette =
        (e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey);
      if (isTyping && !isPalette && e.key !== "Escape") return;
      handler(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
