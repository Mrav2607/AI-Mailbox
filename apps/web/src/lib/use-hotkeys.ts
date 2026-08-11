import { useEffect } from "react";

type Handler = (e: KeyboardEvent) => void;

// The thread/agenda rows are themselves <button>s, so they'd otherwise get
// caught by the "let Enter activate the button natively" rule below — but
// Enter on a row means "open this thread", which has to keep reaching the
// console handler.
const LIST_ROW_SELECTOR = "[data-thread-row], [data-action-row]";

export function shouldSuppressConsoleHotkeys(
  paletteOpen: boolean,
  shortcutsOpen: boolean,
  tourActive: boolean,
  otherOverlayOpen = false,
): boolean {
  return paletteOpen || shortcutsOpen || tourActive || otherOverlayOpen;
}

export function useHotkeys(handler: Handler, deps: unknown[] = []) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;

      // Non-console overlays (command palette, cheatsheet, AI settings) are
      // Radix dialogs. Callers are supposed to track their own "is an
      // overlay open" state and pass it into shouldSuppressConsoleHotkeys,
      // but this DOM check is a backstop in case one gets missed — anything
      // living inside role="dialog" owns its own keyboard handling.
      if (t?.closest('[role="dialog"]')) return;

      const tag = t?.tagName;
      // A <select> owns arrows, type-ahead letters, Enter, and Escape
      // outright — no carve-out here like the one below for text inputs.
      if (tag === "SELECT") return;

      const isTyping =
        tag === "INPUT" || tag === "TEXTAREA" || t?.isContentEditable;
      // Always allow Cmd/Ctrl-K and Escape even while typing
      const isPalette =
        (e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey);
      if (isTyping && !isPalette && e.key !== "Escape") return;

      // Let Enter/Space activate a focused button, link, or summary
      // natively instead of hijacking it — unless it's a list row, where
      // Enter is the "open this thread" hotkey.
      if (e.key === "Enter" || e.key === " ") {
        const interactive = t?.closest("button, a, summary");
        if (interactive && !interactive.closest(LIST_ROW_SELECTOR)) return;
      }

      handler(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
