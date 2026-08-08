import { BrainCircuit, Mail, MessagesSquare } from "lucide-react";

import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import type { Label } from "@/lib/types";

const STATS: { icon: typeof Mail; label: string; value: number }[] = [
  { icon: MessagesSquare, label: "threads", value: 128 },
  { icon: Mail, label: "msgs", value: 342 },
  { icon: BrainCircuit, label: "classified", value: 121 },
];

// Invented placeholder mail, not real messages -- just enough variety to show
// off the label spread and the confidence bar at a glance.
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
    subject: "Re: contract redline — final pass?",
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
    label: "fyi",
    confidence: 0.74,
    subject: "Sprint notes — nothing blocking",
    time: "3h",
  },
  {
    label: "promotional",
    confidence: 0.62,
    subject: "20% off your next renewal",
    time: "1d",
  },
];

/**
 * A frozen snapshot of the triage console, used on the landing page to show
 * what "keyboard-speed triage" actually looks like. Purely decorative: it's
 * `aria-hidden` and non-interactive, with a plain-text caption next to it
 * carrying the real content for anyone using a screen reader.
 */
export function LandingConsoleMockup() {
  return (
    <div
      aria-hidden="true"
      className="w-full max-w-full select-none overflow-hidden rounded-lg border border-border bg-[var(--color-panel)] elevated"
    >
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border bg-[var(--color-panel-hi)] px-3 py-2">
        {STATS.map((s) => (
          <div
            key={s.label}
            className="flex items-center gap-1.5 rounded border border-border bg-[var(--color-panel)] px-2 py-1 font-mono"
          >
            <s.icon className="h-3 w-3 shrink-0 text-muted-foreground/80" />
            <span className="text-[10px] text-muted-foreground">{s.label}</span>
            <span className="text-[11px] tabular-nums">{s.value}</span>
          </div>
        ))}
        <div className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <span className="kbd">⌘</span>
          <span className="kbd">K</span>
        </div>
      </div>
      <ul className="divide-y divide-border">
        {ROWS.map((row) => {
          const meta = LABEL_META[row.label];
          const pct = Math.round(row.confidence * 100);
          return (
            <li
              key={row.subject}
              className={[
                "flex items-center gap-2.5 border-l-2 px-3 py-2",
                row.selected
                  ? "border-primary bg-[var(--color-panel-hi)]"
                  : "border-transparent",
              ].join(" ")}
            >
              <span className="flex w-[84px] shrink-0 items-center gap-1.5 font-mono">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
                <span className={`truncate text-[11px] ${meta.text}`}>{meta.name}</span>
              </span>
              <span className="flex w-16 shrink-0 items-center gap-1.5">
                <span className="h-[2px] w-9 overflow-hidden bg-border">
                  <span
                    className={`block h-full ${confidenceColor(row.confidence)}`}
                    style={{ width: `${pct}%` }}
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
            </li>
          );
        })}
      </ul>
    </div>
  );
}
