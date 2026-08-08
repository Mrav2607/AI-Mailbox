import { BrainCircuit, Mail, MessagesSquare } from "lucide-react";
import { useState } from "react";

import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import type { Label } from "@/lib/types";

const STATS: { icon: typeof Mail; label: string; value: number }[] = [
  { icon: MessagesSquare, label: "threads", value: 128 },
  { icon: Mail, label: "msgs", value: 342 },
  { icon: BrainCircuit, label: "classified", value: 121 },
];

// The six labels in fixed keyboard order (1-6) -- the chip bar below binds
// to this same order, mirroring the real console's bucket bar.
const LABELS = Object.keys(LABEL_META) as Label[];

// Invented placeholder mail, not real messages -- one row per label so every
// chip in the filter bar below has a match.
const ROWS: {
  label: Label;
  confidence: number;
  subject: string;
  time: string;
  selected?: boolean;
}[] = [
  {
    label: "needs_reply",
    confidence: 0.93,
    subject: "Re: contract redline, final pass?",
    time: "2m",
    selected: true,
  },
  {
    label: "action_required",
    confidence: 0.81,
    subject: "Invoice #4821 due Friday",
    time: "1h",
  },
  {
    label: "security_alert",
    confidence: 0.88,
    subject: "New sign-in from Chrome on Ubuntu",
    time: "5m",
  },
  {
    label: "fyi",
    confidence: 0.74,
    subject: "Sprint notes, nothing blocking",
    time: "3h",
  },
  {
    label: "promotional",
    confidence: 0.62,
    subject: "20% off your next renewal",
    time: "1d",
  },
  {
    label: "spam",
    confidence: 0.97,
    subject: "You've been selected for a $500 reward",
    time: "6h",
  },
];

// Stagger + bar-fill timing: rows enter ~80ms apart, each bar starts filling
// once its row has mostly settled.
const ROW_STAGGER_MS = 80;
const BAR_START_OFFSET_MS = 200;

/**
 * A frozen snapshot of the triage console, used on the landing page to show
 * what "keyboard-speed triage" actually looks like. The stats header and the
 * row list stay `aria-hidden` and non-interactive (a plain-text caption next
 * to the mockup carries the real content for screen readers), but the label
 * filter chips are real, focusable buttons -- hovering, focusing, or
 * clicking one highlights the matching row(s) so the interaction demos the
 * real console's bucket bar.
 */
export function LandingConsoleMockup() {
  const [hoverLabel, setHoverLabel] = useState<Label | null>(null);
  const [stickyLabel, setStickyLabel] = useState<Label | null>(null);
  const activeLabel = stickyLabel ?? hoverLabel;

  return (
    <div className="relative">
      {/* Ambient CRT glow, purely decorative -- sits behind the panel via
          -z-10 within this wrapper's own stacking context, so it never
          competes with the panel's 1px border for "the" elevation cue. */}
      <div
        aria-hidden="true"
        className="landing-console-glow pointer-events-none absolute -inset-8 -z-10 rounded-full blur-2xl"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in oklab, var(--primary) 22%, transparent) 0%, transparent 72%)",
        }}
      />
      {/* This is a screenshot-style preview of the real console, which always
          runs the dark "phosphor terminal" palette -- it stays dark on
          purpose even when the landing page itself is in light "paper
          terminal" mode. `dark` re-scopes index.css's `.dark { --var: ... }`
          block for this subtree. Note this only re-scopes the RAW vars
          (--panel, --border, etc.) -- the `--color-*` names from `@theme
          inline` alias to `var(--x)` and resolve once at :root, so any
          arbitrary-value class here has to reference the raw var directly or
          it'll stay stuck on the light value. */}
      <div className="dark w-full max-w-full select-none overflow-hidden rounded-lg border border-border bg-[var(--panel)] text-foreground">
        <div
          aria-hidden="true"
          className="flex flex-wrap items-center gap-1.5 border-b border-border bg-[var(--panel-hi)] px-3 py-2"
        >
          {STATS.map((s) => (
            <div
              key={s.label}
              className="flex items-center gap-1.5 rounded border border-border bg-[var(--panel)] px-2 py-1 font-mono"
            >
              <s.icon className="h-3 w-3 shrink-0 text-muted-foreground/80" />
              <span className="text-[10px] text-muted-foreground">{s.label}</span>
              <span className="text-[11px] tabular-nums">{s.value}</span>
            </div>
          ))}
          <div className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] text-muted-foreground">
            <span className="kbd">⌘</span>
            <span className="kbd">K</span>
            <span className="landing-cursor ml-1 text-primary" aria-hidden="true">
              ▍
            </span>
          </div>
        </div>
        <div
          role="group"
          aria-label="Preview: filter the console by label"
          className="flex flex-wrap items-center gap-1.5 border-b border-border bg-[var(--panel-hi)]/60 px-3 py-1.5"
        >
          {LABELS.map((label) => {
            const meta = LABEL_META[label];
            const isPressed = stickyLabel === label;
            return (
              <button
                key={label}
                type="button"
                aria-pressed={isPressed}
                aria-label={`Filter preview by ${meta.name}`}
                onMouseEnter={() => setHoverLabel(label)}
                onMouseLeave={() => setHoverLabel(null)}
                onFocus={() => setHoverLabel(label)}
                onBlur={() => setHoverLabel(null)}
                onClick={() => setStickyLabel((prev) => (prev === label ? null : label))}
                className={[
                  "flex cursor-pointer items-center gap-1 rounded border px-1.5 py-1 font-mono text-[10px] transition-colors duration-150 ease-out",
                  isPressed
                    ? "border-primary bg-[var(--panel)]"
                    : "border-border bg-[var(--panel)] hover:bg-accent",
                ].join(" ")}
              >
                <span className="kbd">{meta.key}</span>
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
                <span className={meta.text}>{meta.name}</span>
              </button>
            );
          })}
        </div>
        <ul aria-hidden="true" className="divide-y divide-border">
          {ROWS.map((row, i) => {
            const meta = LABEL_META[row.label];
            const pct = Math.round(row.confidence * 100);
            const isActive = activeLabel === row.label;
            const isDimmed = activeLabel !== null && !isActive;
            const rowDelay = i * ROW_STAGGER_MS;
            const barDelay = rowDelay + BAR_START_OFFSET_MS;
            return (
              <li
                key={row.subject}
                className={[
                  "border-l-2 transition-[opacity,box-shadow,background-color] duration-150 ease-out",
                  row.selected ? "border-primary bg-[var(--panel-hi)]" : "border-transparent",
                  isActive ? "ring-1 ring-inset ring-primary/60" : "",
                  isDimmed ? "opacity-45" : "",
                ].join(" ")}
              >
                {/* Entrance choreography lives on this inner element, not the
                    <li>, so its `animation-fill-mode: both` opacity hold
                    (which outranks a plain author `opacity` class in the
                    cascade) never fights the hover/focus dim state above. */}
                <div
                  className="landing-row-in flex items-center gap-2.5 px-3 py-2"
                  style={{ animationDelay: `${rowDelay}ms` }}
                >
                  <span className="flex w-[84px] shrink-0 items-center gap-1.5 font-mono">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
                    <span className={`truncate text-[11px] ${meta.text}`}>{meta.name}</span>
                  </span>
                  <span className="flex w-16 shrink-0 items-center gap-1.5">
                    <span className="h-[2px] w-9 overflow-hidden bg-border">
                      <span
                        className={`landing-bar-fill block h-full ${confidenceColor(row.confidence)}`}
                        style={{ width: `${pct}%`, animationDelay: `${barDelay}ms` }}
                      />
                    </span>
                    <span
                      className={`text-[10px] font-mono tabular-nums ${confidenceText(row.confidence)}`}
                    >
                      {pct}%
                    </span>
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12px] text-foreground/90">
                    {row.subject}
                  </span>
                  <span className="shrink-0 text-[10.5px] font-mono tabular-nums text-muted-foreground">
                    {row.time}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
