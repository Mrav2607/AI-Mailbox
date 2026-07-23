export function relTime(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d`;
  return new Date(iso).toLocaleDateString();
}

export function absTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

export type DateGroup = "today" | "yesterday" | "this week" | "this month" | "older";

// Calendar-day boundaries, not fixed 24h multiples, so this stays correct
// across DST transitions.
function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function daysBefore(start: Date, n: number): Date {
  return new Date(start.getFullYear(), start.getMonth(), start.getDate() - n);
}

export function dateGroup(iso: string | null, now: Date = new Date()): DateGroup {
  if (!iso) return "older";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "older";

  const startOfToday = startOfDay(now).getTime();
  if (t >= startOfToday) return "today";

  const startOfYesterday = daysBefore(startOfDay(now), 1).getTime();
  if (t >= startOfYesterday) return "yesterday";

  const startOfWeekFloor = daysBefore(startOfDay(now), 6).getTime();
  if (t >= startOfWeekFloor) return "this week";

  const startOfMonthFloor = daysBefore(startOfDay(now), 29).getTime();
  if (t >= startOfMonthFloor) return "this month";

  return "older";
}
