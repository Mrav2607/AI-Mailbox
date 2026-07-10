// Theme preference handling. The palette lives in index.css (:root = light,
// .dark = dark); all we do here is keep the `dark` class on <html> in sync.
// index.html runs a tiny inline copy of this before first paint.

export type ThemePref = "dark" | "light" | "system";

export const THEME_PREFS: ThemePref[] = ["system", "light", "dark"];

export function isThemePref(v: unknown): v is ThemePref {
  return v === "dark" || v === "light" || v === "system";
}

const systemQuery = () => window.matchMedia("(prefers-color-scheme: dark)");

export function resolveTheme(pref: ThemePref): "dark" | "light" {
  if (pref === "system") return systemQuery().matches ? "dark" : "light";
  return pref;
}

export function applyTheme(pref: ThemePref) {
  const dark = resolveTheme(pref) === "dark";
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
}

// Re-applies while the preference is "system" and the OS theme flips; the
// callback lets React state (e.g. the toaster's theme) follow along.
// Returns an unsubscribe for the effect cleanup.
export function watchSystemTheme(
  pref: ThemePref,
  onFlip?: (resolved: "dark" | "light") => void,
): () => void {
  if (pref !== "system") return () => {};
  const q = systemQuery();
  const onChange = () => {
    applyTheme("system");
    onFlip?.(q.matches ? "dark" : "light");
  };
  q.addEventListener("change", onChange);
  return () => q.removeEventListener("change", onChange);
}
