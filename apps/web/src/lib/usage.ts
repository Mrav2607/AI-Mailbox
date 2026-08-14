import type { LlmProvider, LlmUsage, LlmUsageDailyPoint } from "./types";

// The UI's own words for each stage -- never the internal "classification" /
// "extraction" column values the usage response actually carries. Shared by
// the settings modal and the usage card so both speak the same language.
export const STAGE_LABELS: Record<string, string> = {
  classification: "Sorting",
  extraction: "Agenda",
  reply_draft: "AI drafts",
};

export const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
  openrouter: "OpenRouter",
  groq: "Groq",
  mistral: "Mistral",
  custom: "Custom endpoint",
};

// Only providers we're confident actually resolve to a real usage/billing
// page. Deliberately partial: guessing a URL that 404s is worse than no link
// at all, and "custom" has no dashboard we could point to anyway.
//
// OpenAI and OpenRouter get a deep link straight to the usage page. The rest
// point at the console home instead: those products move their billing pages
// around, and a stale deep link that 404s is worse than one extra click.
export const USAGE_DASHBOARD_URLS: Partial<Record<LlmProvider, string>> = {
  openai: "https://platform.openai.com/usage",
  openrouter: "https://openrouter.ai/activity",
  gemini: "https://aistudio.google.com/",
  groq: "https://console.groq.com/",
  mistral: "https://console.mistral.ai/",
};

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// UTC calendar-day string ("YYYY-MM-DD"), matching how the API stamps
// `usage_date`. Never use local getFullYear/getMonth/getDate here -- that's
// exactly the bug this function exists to avoid.
function utcDateString(epochMs: number): string {
  return new Date(epochMs).toISOString().slice(0, 10);
}

/**
 * Expands the API's sparse `daily` array into one entry per calendar day in
 * the window, zero-filling any day the API omitted.
 *
 * The API only returns rows for days that actually had usage
 * (`usage_date >= today - (days - 1)`, UTC calendar day). Plotting those
 * rows adjacently would draw three scattered active days as one solid block
 * and materially misrepresent how often the key gets used -- this is the
 * correctness heart of the chart.
 *
 * `today` is a required parameter (never `new Date()` read inline) so the
 * window boundary is deterministic and testable. The window ends on
 * `today`'s UTC calendar day and runs backward `windowDays` days, matching
 * the API's own cutoff convention exactly.
 */
export function fillDailySeries(
  daily: LlmUsageDailyPoint[],
  windowDays: number,
  today: Date,
): LlmUsageDailyPoint[] {
  const byDate = new Map(daily.map((d) => [d.date, d]));
  const todayUtcMidnight = Date.UTC(
    today.getUTCFullYear(),
    today.getUTCMonth(),
    today.getUTCDate(),
  );
  const cutoff = todayUtcMidnight - (windowDays - 1) * MS_PER_DAY;

  const out: LlmUsageDailyPoint[] = [];
  for (let i = 0; i < windowDays; i++) {
    const date = utcDateString(cutoff + i * MS_PER_DAY);
    out.push(byDate.get(date) ?? { date, calls: 0, total_tokens: 0 });
  }
  return out;
}

export interface UsageSummary {
  // Count of days in the window with at least one call -- read off the
  // API's own sparse `daily` (every row it sends already represents a day
  // with real usage), not the zero-filled series.
  activeDays: number;
  busiestDay: LlmUsageDailyPoint | null;
  avgCallsPerActiveDay: number;
  // calls_with_total_tokens / calls, in [0, 1]. 0 (never NaN) when there were
  // no calls at all.
  tokenCoverage: number;
}

/**
 * Derives the small set of headline numbers the usage card's stat tiles show,
 * guarding every division so a zero-call window renders 0s instead of NaN.
 */
export function summarizeUsage(usage: LlmUsage): UsageSummary {
  const activeDays = usage.daily.filter((d) => d.calls > 0).length;
  const busiestDay = usage.daily.reduce<LlmUsageDailyPoint | null>(
    (busiest, d) => (!busiest || d.calls > busiest.calls ? d : busiest),
    null,
  );
  const avgCallsPerActiveDay = activeDays > 0 ? usage.totals.calls / activeDays : 0;
  const tokenCoverage =
    usage.totals.calls > 0 ? usage.totals.calls_with_total_tokens / usage.totals.calls : 0;
  return { activeDays, busiestDay, avgCallsPerActiveDay, tokenCoverage };
}

/**
 * Rounds a series' max up to a "nice" axis ceiling (1/2/5 x a power of ten)
 * instead of the raw max, so the chart's gridlines land on round numbers
 * instead of scaling bars against something like 37. Always returns a
 * positive number -- an all-zero series still needs a ceiling to divide bar
 * heights by.
 */
export function chartMax(values: number[]): number {
  const max = values.length > 0 ? Math.max(...values) : 0;
  if (max <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  const normalized = max / magnitude;
  const niceNormalized = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return niceNormalized * magnitude;
}
