import type { BucketKey, Label } from "./types";

/*
  Phosphor Terminal label system. Color lives in a single dot and a tinted
  label word — never a filled pill — so the list scans by hue without any one
  label shouting. The six hues are tuned to roughly equal lightness/chroma so
  weight reads evenly; spam is drained toward gray and fyi runs low-chroma
  because they're the ones you can safely ignore. Amber (hue 80) is deliberately
  absent here — it's reserved for the UI's selection/primary accent.

  - dot:    solid swatch color (the dot, sidebar chip, confidence fill)
  - text:   tinted label word, still ~4.5:1 on the panel
  - soft:   low-alpha background for the one chip that earns a fill (prediction)
  - border: tinted hairline for that same chip
*/
// Values live as CSS variables in index.css (per-theme, light + dark) so the
// six hues can be tuned for contrast on both palettes without touching code.
export const LABEL_META: Record<
  Label,
  { name: string; dot: string; text: string; soft: string; border: string; key: string }
> = {
  needs_reply: {
    name: "needs reply",
    dot: "bg-[var(--lbl-needs-reply-dot)]",
    text: "text-[var(--lbl-needs-reply-text)]",
    soft: "bg-[var(--lbl-needs-reply-soft)]",
    border: "border-[var(--lbl-needs-reply-border)]",
    key: "1",
  },
  action_required: {
    name: "action req",
    dot: "bg-[var(--lbl-action-required-dot)]",
    text: "text-[var(--lbl-action-required-text)]",
    soft: "bg-[var(--lbl-action-required-soft)]",
    border: "border-[var(--lbl-action-required-border)]",
    key: "2",
  },
  fyi: {
    name: "fyi",
    dot: "bg-[var(--lbl-fyi-dot)]",
    text: "text-[var(--lbl-fyi-text)]",
    soft: "bg-[var(--lbl-fyi-soft)]",
    border: "border-[var(--lbl-fyi-border)]",
    key: "3",
  },
  promotional: {
    name: "promo",
    dot: "bg-[var(--lbl-promotional-dot)]",
    text: "text-[var(--lbl-promotional-text)]",
    soft: "bg-[var(--lbl-promotional-soft)]",
    border: "border-[var(--lbl-promotional-border)]",
    key: "4",
  },
  security_alert: {
    name: "security",
    dot: "bg-[var(--lbl-security-alert-dot)]",
    text: "text-[var(--lbl-security-alert-text)]",
    soft: "bg-[var(--lbl-security-alert-soft)]",
    border: "border-[var(--lbl-security-alert-border)]",
    key: "5",
  },
  spam: {
    name: "spam",
    dot: "bg-[var(--lbl-spam-dot)]",
    text: "text-[var(--lbl-spam-text)]",
    soft: "bg-[var(--lbl-spam-soft)]",
    border: "border-[var(--lbl-spam-border)]",
    key: "6",
  },
};

export const BUCKET_KEYS: Record<BucketKey, string> = {
  needs_reply: "1",
  action_required: "2",
  fyi: "3",
  promotional: "4",
  security_alert: "5",
  spam: "6",
  all: "7",
  unclassified: "8",
};

export function bucketLabel(b: BucketKey): string {
  if (b === "all") return "all";
  if (b === "unclassified") return "unclassified";
  return LABEL_META[b].name;
}

// Confidence keeps a universal green/amber/red read, muted to sit inside the
// terminal palette rather than glow like a status LED.
export function confidenceColor(c: number | null): string {
  if (c == null) return "bg-muted";
  if (c >= 0.8) return "bg-[var(--conf-hi)]";
  if (c >= 0.5) return "bg-[var(--conf-mid)]";
  return "bg-[var(--conf-low)]";
}
export function confidenceText(c: number | null): string {
  if (c == null) return "text-muted-foreground";
  if (c >= 0.8) return "text-[var(--conf-hi-text)]";
  if (c >= 0.5) return "text-[var(--conf-mid-text)]";
  return "text-[var(--conf-low-text)]";
}
