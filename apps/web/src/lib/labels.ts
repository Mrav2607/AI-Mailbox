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
export const LABEL_META: Record<
  Label,
  { name: string; dot: string; text: string; soft: string; border: string; key: string }
> = {
  needs_reply: {
    name: "needs reply",
    dot: "bg-[oklch(0.66_0.18_22)]",
    text: "text-[oklch(0.78_0.14_22)]",
    soft: "bg-[oklch(0.66_0.18_22_/_0.12)]",
    border: "border-[oklch(0.66_0.18_22_/_0.45)]",
    key: "1",
  },
  action_required: {
    name: "action req",
    dot: "bg-[oklch(0.74_0.15_52)]",
    text: "text-[oklch(0.82_0.12_54)]",
    soft: "bg-[oklch(0.74_0.15_52_/_0.12)]",
    border: "border-[oklch(0.74_0.15_52_/_0.45)]",
    key: "2",
  },
  fyi: {
    name: "fyi",
    dot: "bg-[oklch(0.68_0.09_235)]",
    text: "text-[oklch(0.78_0.08_235)]",
    soft: "bg-[oklch(0.68_0.09_235_/_0.12)]",
    border: "border-[oklch(0.68_0.09_235_/_0.45)]",
    key: "3",
  },
  promotional: {
    name: "promo",
    dot: "bg-[oklch(0.66_0.13_322)]",
    text: "text-[oklch(0.77_0.11_322)]",
    soft: "bg-[oklch(0.66_0.13_322_/_0.12)]",
    border: "border-[oklch(0.66_0.13_322_/_0.45)]",
    key: "4",
  },
  security_alert: {
    name: "security",
    dot: "bg-[oklch(0.64_0.21_12)]",
    text: "text-[oklch(0.76_0.17_14)]",
    soft: "bg-[oklch(0.64_0.21_12_/_0.12)]",
    border: "border-[oklch(0.64_0.21_12_/_0.45)]",
    key: "5",
  },
  spam: {
    name: "spam",
    dot: "bg-[oklch(0.55_0.015_260)]",
    text: "text-[oklch(0.66_0.012_260)]",
    soft: "bg-[oklch(0.55_0.015_260_/_0.14)]",
    border: "border-[oklch(0.55_0.015_260_/_0.5)]",
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
  if (c >= 0.8) return "bg-[oklch(0.72_0.15_150)]";
  if (c >= 0.5) return "bg-[oklch(0.78_0.13_82)]";
  return "bg-[oklch(0.66_0.19_25)]";
}
export function confidenceText(c: number | null): string {
  if (c == null) return "text-muted-foreground";
  if (c >= 0.8) return "text-[oklch(0.78_0.13_150)]";
  if (c >= 0.5) return "text-[oklch(0.82_0.12_82)]";
  return "text-[oklch(0.74_0.17_25)]";
}
