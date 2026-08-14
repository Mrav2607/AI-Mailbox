// Snooze preset math (docs/plans/2026-08-13-snooze-plan.md §3.4/§3.6).
// Every resolver takes the current instant as a plain argument (never reads
// `Date.now()` itself) so tests can inject a fixed clock, incl. DST
// boundaries. All resolution happens in the caller's LOCAL time -- the
// server only ever sees the resolved absolute instant (an ISO string with
// its own offset), never a timezone.
//
// P1-11 (frozen): every preset resolves to its NEXT FUTURE occurrence.
// "This weekend" clicked mid-weekend means next weekend, never a past
// instant the server's 422 would reject. Component-based Date construction
// (year, month, day, hour, ...) is what keeps this DST-safe -- it always
// means "this wall-clock time on this date", unlike adding a fixed number
// of milliseconds, which would drift by an hour across a transition.

export type SnoozePresetId = "later_today" | "tomorrow" | "weekend" | "next_week";

const DAY_MS = 24 * 60 * 60 * 1000;

// +3h from now. Always future -- the server's minimum lead (60s) is nowhere
// close, so this never needs a rollover check.
export function laterToday(now: Date): Date {
  return new Date(now.getTime() + 3 * 60 * 60 * 1000);
}

// Tomorrow's date at 08:00 local time. Always at least a few hours out
// (worst case: clicked at 07:59, still >23h away), so this never needs a
// rollover check either.
export function tomorrowMorning(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0, 0);
}

// The next Saturday at 09:00 local time. If today already IS Saturday and
// 09:00 has already passed, "this weekend" rolls to next Saturday rather
// than landing in the past -- but a Saturday morning before 09:00 still
// resolves to later today, since that's still in the future and still
// "this weekend" in the ordinary sense of the phrase.
export function thisWeekend(now: Date): Date {
  const SATURDAY = 6;
  const daysUntil = (SATURDAY - now.getDay() + 7) % 7;
  let candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + daysUntil, 9, 0, 0, 0);
  if (candidate.getTime() <= now.getTime()) {
    candidate = new Date(candidate.getFullYear(), candidate.getMonth(), candidate.getDate() + 7, 9, 0, 0, 0);
  }
  return candidate;
}

// The next Monday at 08:00 local time. Unlike "this weekend", "next week"
// implies leaving the CURRENT week entirely -- clicked on a Monday, this
// always means the Monday after, never later today, regardless of what
// time it currently is.
export function nextWeek(now: Date): Date {
  const MONDAY = 1;
  let daysUntil = (MONDAY - now.getDay() + 7) % 7;
  if (daysUntil === 0) daysUntil = 7;
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + daysUntil, 8, 0, 0, 0);
}

export interface SnoozePresetDef {
  id: SnoozePresetId;
  label: string;
  resolve: (now: Date) => Date;
}

// Rendered by ThreadDetailPane's snooze popover, in this order. "Pick
// date…" (a native datetime-local input) isn't a preset -- it's the
// popover's own free-form fallback, kept out of this table.
export const SNOOZE_PRESETS: SnoozePresetDef[] = [
  { id: "later_today", label: "Later today", resolve: laterToday },
  { id: "tomorrow", label: "Tomorrow 8:00", resolve: tomorrowMorning },
  { id: "weekend", label: "This weekend", resolve: thisWeekend },
  { id: "next_week", label: "Next week", resolve: nextWeek },
];

// The snoozed bucket's wake-time column: a relative phrase ("wakes
// tomorrow 08:00") instead of the bucket list's usual relative-past time,
// since this list answers "what comes back next" (§3.2's sort contract),
// not "what happened most recently". Calendar-day arithmetic (not a fixed
// 24h multiple), same rule dateGroup in lib/time.ts follows, so this stays
// correct across DST.
export function formatWakeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (dayDiff <= 0) return `wakes today ${time}`;
  if (dayDiff === 1) return `wakes tomorrow ${time}`;
  if (dayDiff < 7) return `wakes ${d.toLocaleDateString([], { weekday: "long" })} ${time}`;
  return `wakes ${d.toLocaleDateString()} ${time}`;
}
