import type { BucketKey, Label } from "./types";

export const LABEL_META: Record<
  Label,
  { name: string; chip: string; bar: string; border: string; text: string; key: string }
> = {
  needs_reply: {
    name: "needs reply",
    chip: "bg-[oklch(0.55_0.22_25)] text-white",
    bar: "bg-[oklch(0.65_0.22_25)]",
    border: "border-[oklch(0.65_0.22_25)]",
    text: "text-[oklch(0.75_0.18_25)]",
    key: "1",
  },
  action_required: {
    name: "action req",
    chip: "bg-[oklch(0.6_0.2_50)] text-white",
    bar: "bg-[oklch(0.7_0.2_50)]",
    border: "border-[oklch(0.7_0.2_50)]",
    text: "text-[oklch(0.8_0.18_50)]",
    key: "2",
  },
  fyi: {
    name: "fyi",
    chip: "bg-[oklch(0.55_0.15_240)] text-white",
    bar: "bg-[oklch(0.65_0.18_240)]",
    border: "border-[oklch(0.65_0.18_240)]",
    text: "text-[oklch(0.75_0.16_240)]",
    key: "3",
  },
  promotional: {
    name: "promo",
    chip: "bg-[oklch(0.55_0.18_300)] text-white",
    bar: "bg-[oklch(0.65_0.2_300)]",
    border: "border-[oklch(0.65_0.2_300)]",
    text: "text-[oklch(0.75_0.18_300)]",
    key: "4",
  },
  security_alert: {
    name: "security",
    chip: "bg-[oklch(0.55_0.22_15)] text-white",
    bar: "bg-[oklch(0.65_0.22_15)]",
    border: "border-[oklch(0.65_0.22_15)]",
    text: "text-[oklch(0.78_0.2_15)]",
    key: "5",
  },
  spam: {
    name: "spam",
    chip: "bg-[oklch(0.45_0.05_260)] text-white",
    bar: "bg-[oklch(0.55_0.05_260)]",
    border: "border-[oklch(0.55_0.05_260)]",
    text: "text-[oklch(0.7_0.04_260)]",
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

export function confidenceColor(c: number | null): string {
  if (c == null) return "bg-muted";
  if (c >= 0.8) return "bg-[oklch(0.7_0.18_145)]";
  if (c >= 0.5) return "bg-[oklch(0.75_0.17_75)]";
  return "bg-[oklch(0.65_0.22_25)]";
}
export function confidenceText(c: number | null): string {
  if (c == null) return "text-muted-foreground";
  if (c >= 0.8) return "text-[oklch(0.8_0.18_145)]";
  if (c >= 0.5) return "text-[oklch(0.82_0.17_75)]";
  return "text-[oklch(0.78_0.2_25)]";
}
