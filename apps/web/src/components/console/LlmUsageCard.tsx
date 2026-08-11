import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import type { LlmUsage, LlmUsageByProvider, LlmUsageByStage } from "@/lib/types";
import {
  chartMax,
  fillDailySeries,
  PROVIDER_LABELS,
  STAGE_LABELS,
  summarizeUsage,
  USAGE_DASHBOARD_URLS,
} from "@/lib/usage";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // Same tri-state contract as LlmSettings' usage prop: null while a fetch is
  // in flight (or hasn't started), a real payload once one lands, and
  // `usageError` set on a failed fetch -- an outage here must never look
  // like a broken app.
  usage: LlmUsage | null;
  usageError: boolean;
  days: number;
  onDaysChange: (days: number) => void;
}

const RANGE_CHOICES = [7, 30, 90] as const;

const fieldLabel = "font-mono text-[11px] text-muted-foreground";

// Cycles through varying opacities of the single amber accent, plus a muted
// tone for whatever's left over -- there's one accent color in this UI, so
// segments are told apart by weight, not hue.
const SEGMENT_TONES = ["bg-primary", "bg-primary/55", "bg-primary/30", "bg-muted-foreground/50"];

function formatDayLabel(dateStr: string): string {
  // `dateStr` is a UTC calendar day ("YYYY-MM-DD"); pin the formatter to UTC
  // too, or a viewer west of UTC would see it roll back a day.
  return new Date(`${dateStr}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// Turns a set of {label, calls} counters into a horizontal proportion bar's
// segments -- shared shape for the by-stage and by-provider breakdowns below.
function splitBarSegments(entries: { label: string; calls: number }[]) {
  const total = entries.reduce((sum, e) => sum + e.calls, 0);
  return entries
    .filter((e) => e.calls > 0)
    .map((e, i) => ({
      ...e,
      pct: total > 0 ? (e.calls / total) * 100 : 0,
      tone: SEGMENT_TONES[i % SEGMENT_TONES.length],
    }));
}

function SplitBar({
  title,
  entries,
}: {
  title: string;
  entries: { label: string; calls: number }[];
}) {
  const segments = splitBarSegments(entries);
  if (segments.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <span className={fieldLabel}>{title}</span>
      <div className="h-2.5 w-full rounded-full bg-muted overflow-hidden flex">
        {segments.map((s) => (
          <div
            key={s.label}
            className={s.tone}
            style={{ width: `${s.pct}%` }}
            title={`${s.label}: ${s.calls.toLocaleString()} calls`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {segments.map((s) => (
          <span
            key={s.label}
            className="inline-flex items-center gap-1.5 text-[10.5px] font-mono text-muted-foreground"
          >
            <span className={`h-1.5 w-1.5 rounded-full ${s.tone}`} />
            {s.label} · {s.calls.toLocaleString()}
          </span>
        ))}
      </div>
    </div>
  );
}

function DailyBarChart({ usage }: { usage: LlmUsage }) {
  const filled = fillDailySeries(usage.daily, usage.window_days, new Date());
  const max = chartMax(filled.map((d) => d.calls));
  const chartHeight = 96;
  const gap = filled.length > 45 ? 0.5 : 2;
  const slot = 100 / filled.length;
  // Cap how wide a bar can get and centre it in its slot. Without this a
  // 7-day window turns each bar into a slab nearly touching its neighbour,
  // which reads as a block of colour rather than a chart.
  const barWidth = Math.min(Math.max(slot - gap, 0.4), 6);
  const totalCalls = filled.reduce((sum, d) => sum + d.calls, 0);
  const activeDays = filled.filter((d) => d.calls > 0).length;

  return (
    <div className="space-y-1">
      {/* Scale lives up here with the title rather than centred under the
          chart, where it competed with the two date labels and read like a
          third axis tick. */}
      <div className="flex items-baseline justify-between">
        <span className={fieldLabel}>calls per day</span>
        <span className="text-[10px] font-mono text-muted-foreground">
          peak {max.toLocaleString()}/day
        </span>
      </div>
      <svg
        viewBox={`0 0 100 ${chartHeight}`}
        preserveAspectRatio="none"
        className="w-full h-24"
        role="img"
        aria-label={`Calls per day over the last ${filled.length} days: ${totalCalls.toLocaleString()} calls total across ${activeDays} active day${activeDays === 1 ? "" : "s"}, from ${formatDayLabel(filled[0].date)} to ${formatDayLabel(filled[filled.length - 1].date)}.`}
      >
        {/* Baseline plus two gridlines at half and full axis height. */}
        {[0, 0.5, 1].map((frac) => (
          <line
            key={frac}
            x1={0}
            x2={100}
            y1={chartHeight - frac * chartHeight}
            y2={chartHeight - frac * chartHeight}
            className="stroke-border"
            strokeWidth={0.3}
          />
        ))}
        {filled.map((d, i) => {
          const h = (d.calls / max) * (chartHeight - 2);
          return (
            <rect
              key={d.date}
              x={i * slot + (slot - barWidth) / 2}
              y={chartHeight - h}
              width={barWidth}
              height={h}
              className={d.calls > 0 ? "fill-primary" : "fill-primary/10"}
            >
              <title>
                {formatDayLabel(d.date)}: {d.calls.toLocaleString()} call{d.calls === 1 ? "" : "s"}
              </title>
            </rect>
          );
        })}
      </svg>
      <div className="flex items-center justify-between text-[10px] font-mono text-muted-foreground">
        <span>{formatDayLabel(filled[0].date)}</span>
        <span>{formatDayLabel(filled[filled.length - 1].date)}</span>
      </div>
    </div>
  );
}

function StatTile({ value, caption }: { value: string; caption: string }) {
  return (
    <div className="rounded border border-border bg-[var(--color-panel-hi)] px-2.5 py-2 flex-1 min-w-[92px]">
      <div className="text-[18px] font-mono tabular-nums text-foreground leading-none">
        {value}
      </div>
      <div className="text-[10px] font-mono text-muted-foreground mt-1">{caption}</div>
    </div>
  );
}

export function LlmUsageCard({ open, onOpenChange, usage, usageError, days, onDaysChange }: Props) {
  const summary = usage ? summarizeUsage(usage) : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* This card is read-only, so don't hand focus to the first control on
          open -- that put a focus ring on the 7d button and made it look like
          the selected range. Radix falls back to focusing the dialog itself,
          which is what a screen reader should land on anyway. */}
      <DialogContent
        className="max-w-2xl bg-[var(--color-panel)] border-border"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <DialogTitle className="font-mono text-[11px] tracking-wide text-muted-foreground mb-0.5 font-normal">
          AI usage
        </DialogTitle>
        <p className="text-[11px] font-mono text-muted-foreground leading-relaxed">
          Your account&apos;s background AI usage, tracked across whatever key
          or provider was configured at the time.
        </p>

        <div className="flex items-center gap-1" role="group" aria-label="usage window">
          {RANGE_CHOICES.map((d) => (
            <button
              key={d}
              type="button"
              aria-pressed={days === d}
              onClick={() => onDaysChange(d)}
              className={[
                "h-6 px-2.5 rounded border text-[11px] font-mono cursor-pointer transition-colors",
                // Solid fill for the active range, not a tint: at 15% opacity
                // the selected button was indistinguishable from a merely
                // focused one, so the card looked like it was showing 7 days
                // while actually showing 30.
                days === d
                  ? "border-primary bg-primary text-primary-foreground font-medium"
                  : "border-border bg-[var(--color-panel-hi)] text-muted-foreground hover:text-foreground",
              ].join(" ")}
            >
              {d}d
            </button>
          ))}
        </div>

        {usageError ? (
          <p className="text-[11px] font-mono text-muted-foreground leading-snug py-6 text-center">
            Usage isn&apos;t available right now.
          </p>
        ) : !usage ? (
          <p className="text-[11px] font-mono text-muted-foreground leading-snug py-6 text-center">
            checking your usage…
          </p>
        ) : usage.totals.calls === 0 ? (
          <p className="text-[11px] font-mono text-muted-foreground leading-snug py-6 text-center">
            Your key hasn&apos;t been used yet.
          </p>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <StatTile value={usage.totals.calls.toLocaleString()} caption="total calls" />
              <StatTile
                value={usage.totals.total_tokens.toLocaleString()}
                caption="total tokens"
              />
              <StatTile
                value={String(summary?.activeDays ?? 0)}
                caption={`active day${summary?.activeDays === 1 ? "" : "s"}`}
              />
              <StatTile
                value={
                  summary?.busiestDay
                    ? `${summary.busiestDay.calls.toLocaleString()} · ${formatDayLabel(summary.busiestDay.date)}`
                    : "—"
                }
                caption="busiest day"
              />
            </div>

            <DailyBarChart usage={usage} />

            <SplitBar
              title="by pipeline"
              entries={usage.by_stage.map((s: LlmUsageByStage) => ({
                label: STAGE_LABELS[s.stage] ?? s.stage,
                calls: s.calls,
              }))}
            />
            <SplitBar
              title="by provider"
              entries={usage.by_provider.map((p: LlmUsageByProvider) => ({
                label: PROVIDER_LABELS[p.provider] ?? p.provider,
                calls: p.calls,
              }))}
            />

            {usage.totals.calls_with_total_tokens < usage.totals.calls && (
              <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                {usage.totals.calls_with_total_tokens.toLocaleString()} of{" "}
                {usage.totals.calls.toLocaleString()} calls reported token counts —
                the rest succeeded but the provider didn&apos;t report a number.
              </p>
            )}

            {/* Linked per provider that actually ran in the window, not the one
                configured right now -- after a provider switch those differ,
                and pointing at today's dashboard for last month's calls sends
                people somewhere the money isn't. */}
            <div className="flex flex-col gap-0.5 pt-1 border-t border-border/60">
              {usage.by_provider
                .filter((p) => USAGE_DASHBOARD_URLS[p.provider])
                .map((p) => (
                  <a
                    key={p.provider}
                    href={USAGE_DASHBOARD_URLS[p.provider]}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10.5px] font-mono text-primary underline underline-offset-2"
                  >
                    See what this actually cost on{" "}
                    {PROVIDER_LABELS[p.provider] ?? p.provider}
                  </a>
                ))}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
