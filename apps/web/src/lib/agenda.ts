import type { ActionItem } from "./types";

export type AgendaGroupKey = "overdue" | "today" | "this_week" | "later" | "no_deadline";

export interface AgendaGroup {
  key: AgendaGroupKey;
  label: string;
  items: ActionItem[];
}

const GROUP_ORDER: AgendaGroupKey[] = ["overdue", "today", "this_week", "later", "no_deadline"];

const GROUP_LABELS: Record<AgendaGroupKey, string> = {
  overdue: "Overdue",
  today: "Today",
  this_week: "This week",
  later: "Later",
  no_deadline: "No deadline",
};

// Local-midnight day components for `date`, as a whole-number of days since
// the epoch -- comparing these (rather than subtracting Date instants
// directly) keeps the day-diff exact across DST transitions, since it never
// touches the hour/minute portion of either timestamp.
function dayNumber(y: number, m: number, d: number): number {
  return Math.floor(Date.UTC(y, m, d) / 86_400_000);
}

// The calendar day an item is "due" on, for grouping purposes. A
// `due_precision: "date"` deadline was pinned by the server at 23:59:59 UTC
// of the INTENDED date -- converting that instant to the viewer's local time
// zone can land on the wrong day (e.g. 23:59:59Z read in a UTC-8 zone is
// 15:59:59 the same day, but in a UTC+9 zone it's already 08:59:59 the NEXT
// day). So for date precision we read the day off the UTC components (that's
// the date the server meant), and only fall back to local components for a
// real `datetime` deadline, where the exact instant -- not a calendar-date
// placeholder -- is what matters.
function dueDayNumber(item: ActionItem): number | null {
  if (!item.due_at) return null;
  const due = new Date(item.due_at);
  return item.due_precision === "date"
    ? dayNumber(due.getUTCFullYear(), due.getUTCMonth(), due.getUTCDate())
    : dayNumber(due.getFullYear(), due.getMonth(), due.getDate());
}

/**
 * Groups agenda items into Overdue / Today / This week / Later / No deadline
 * for the console's board, in that fixed display order. Grouping is by
 * calendar day only (never by exact time-of-day), computed against `now`'s
 * LOCAL date so the board matches what the viewer would call "today"
 * regardless of the API's UTC timestamps.
 *
 * `now` is a required parameter (never `Date.now()` read inline) so tests
 * can pin "today" and assert every bucket deterministically. Every group key
 * is always present, even with an empty `items` array, so a caller doesn't
 * need to guard against a missing bucket.
 *
 * Two items that share a `thread_id` are never merged or deduped -- each is
 * an independent obligation keyed by its own `id`.
 */
export function groupActions(items: ActionItem[], now: Date): AgendaGroup[] {
  const today = dayNumber(now.getFullYear(), now.getMonth(), now.getDate());
  const buckets: Record<AgendaGroupKey, ActionItem[]> = {
    overdue: [],
    today: [],
    this_week: [],
    later: [],
    no_deadline: [],
  };

  for (const item of items) {
    const due = dueDayNumber(item);
    if (due === null) {
      buckets.no_deadline.push(item);
      continue;
    }
    const diff = due - today;
    if (diff < 0) buckets.overdue.push(item);
    else if (diff === 0) buckets.today.push(item);
    else if (diff <= 6) buckets.this_week.push(item);
    else buckets.later.push(item);
  }

  return GROUP_ORDER.map((key) => ({ key, label: GROUP_LABELS[key], items: buckets[key] }));
}
