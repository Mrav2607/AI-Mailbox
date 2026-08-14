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

// Partial, not exhaustive (docs/plans/2026-08-13-snooze-plan.md §3.6/P1-7):
// "snoozed" deliberately has no digit (P2-3, reached by click or the
// command palette only) -- an exhaustive Record can't express "no
// shortcut" for one key without lying about it having one.
export const BUCKET_KEYS: Partial<Record<BucketKey, string>> = {
  needs_reply: "1",
  action_required: "2",
  fyi: "3",
  promotional: "4",
  security_alert: "5",
  spam: "6",
  all: "7",
  unclassified: "8",
  done: "9",
};

export function bucketLabel(b: BucketKey): string {
  if (b === "all") return "all";
  if (b === "unclassified") return "unclassified";
  if (b === "done") return "done";
  if (b === "snoozed") return "snoozed";
  return LABEL_META[b].name;
}

// Confidence reads as a three-step red -> amber -> green traffic light. Three
// steps, not five: the bar is 3px tall, and squeezing more tiers into the
// lightness range that contrast allows made neighbouring steps as close to
// each other as they were to the label colors -- a distinction nobody could
// see. The exact number is printed beside the bar anyway, so the bar's job is
// just "can I trust this at a glance". See index.css for why the tokens sit
// at low chroma and ramp in lightness.
//
// Thresholds are on the DISPLAYED percentage (Math.round(c * 100)), not the
// raw float -- every call site renders the rounded percentage, so branching on
// the float let a row's color and its printed number land in different tiers
// right at a boundary: 0.3451 and 0.3549 both print "35%", but a raw `c <= 0.35`
// cut puts them in different tiers.
export function confidenceColor(c: number | null): string {
  if (c == null) return "bg-muted";
  const pct = Math.round(c * 100);
  if (pct <= 35) return "bg-[var(--conf-red)]";
  if (pct <= 80) return "bg-[var(--conf-amber)]";
  return "bg-[var(--conf-green)]";
}
export function confidenceText(c: number | null): string {
  if (c == null) return "text-muted-foreground";
  const pct = Math.round(c * 100);
  if (pct <= 35) return "text-[var(--conf-red-text)]";
  if (pct <= 80) return "text-[var(--conf-amber-text)]";
  return "text-[var(--conf-green-text)]";
}

// One thickness, one shape, for the confidence bar everywhere it renders --
// the three call sites used to drift (2px/3px/2px) and looked inconsistent
// once the console's zoom transform rounded each one to different device
// pixels.
// `block` so these work on a <span> as well as a <div>: inside a row <button>
// the markup has to be phrasing content, and an inline box would drop the
// height and the fill's width entirely.
export const CONF_BAR_TRACK = "block h-[3px] shrink-0 rounded-full bg-border overflow-hidden";
// No rounded-full here -- the track's own rounding + overflow-hidden already
// clips the fill's visible left edge, and a corner radius on a 2-3px-tall
// fill eats the whole bar at low confidences (a 5% fill is ~2px wide, all
// corner, nothing painted).
export const CONF_BAR_FILL = "block h-full";
