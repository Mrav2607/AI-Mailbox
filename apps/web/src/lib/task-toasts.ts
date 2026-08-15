// Toast copy for the backfill and action-extraction sweep results
// (docs/plans/2026-08-14-llm-failure-visibility-plan.md). Both jobs can now
// silently fall back to a local model or fail outright when the configured
// LLM is unreachable/rate-limited; a plain "N processed" toast used to hide
// that entirely. These helpers turn the API's raw counts into the frozen
// copy, so App.tsx never has to inline the same category-to-plain-words
// mapping at three separate call sites.

// Plain-words phrasing for LlmCallError categories the API records on
// `failure_categories`. Unmapped/unknown categories are left out of the
// toast rather than surfaced raw (e.g. "http_429") -- the ratio numbers
// already tell the user *that* something degraded.
const FAILURE_CATEGORY_LABELS: Record<string, string> = {
  http_429: "rate limited by your provider",
  timed_out: "provider timed out",
  connection_failed: "could not reach your provider",
  invalid_response: "provider returned an unusable response",
  blocked_by_policy: "blocked by policy",
};

/**
 * Picks the plain-words phrase for whichever failure category accounts for
 * more than half of the recorded failures, or `null` when no single category
 * dominates (a genuine mix) or the API sent nothing/an unmapped category.
 */
function dominantFailureLabel(categories?: Record<string, number>): string | null {
  if (!categories) return null;
  const entries = Object.entries(categories).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  const [topCategory, topCount] = entries.reduce((a, b) => (b[1] > a[1] ? b : a));
  if (topCount / total <= 0.5) return null;
  return FAILURE_CATEGORY_LABELS[topCategory] ?? null;
}

function appendDominantLabel(message: string, categories?: Record<string, number>): string {
  const label = dominantFailureLabel(categories);
  return label ? `${message} — ${label}` : message;
}

export interface ToastOutcome {
  message: string;
  // true routes the caller to toast.warning instead of toast.success.
  warn: boolean;
}

export interface BackfillToastInput {
  created?: number;
  fell_back?: number;
  failure_categories?: Record<string, number>;
}

/**
 * Builds the backfill result toast. `cleanMessage` is the site's own
 * unchanged success copy (the queued and inline backfill call sites use
 * slightly different wording) -- this function only decides whether that
 * copy still applies or the degraded ratio replaces it.
 *
 * Missing fields (an older API/worker result recorded before this change)
 * all default to 0, which reads as a clean run -- exactly today's behaviour.
 */
export function backfillToastOutcome(
  res: BackfillToastInput,
  cleanMessage: string,
): ToastOutcome {
  const fellBack = res.fell_back ?? 0;
  if (fellBack <= 0) return { message: cleanMessage, warn: false };
  const created = res.created ?? 0;
  // "N of those" rather than "N of M": fell_back is a SUBSET of what was
  // classified, and reading it as a separate total would double the apparent
  // work. A literal `${fellBack} of ${created}` would be worse -- a user
  // override landing mid-run increments fell_back but not created, so the
  // denominator can legitimately come out smaller than the numerator.
  const message = appendDominantLabel(
    `classified ${created} · ${fellBack} of those fell back to the built-in model`,
    res.failure_categories,
  );
  return { message, warn: true };
}

export interface ExtractionToastInput {
  extracted?: number;
  failed?: number;
  failure_categories?: Record<string, number>;
}

/**
 * Builds the action-extraction sweep toast. Unlike backfill, `extracted`/
 * `failed` aren't new fields -- the API always returned them, the old toast
 * just read `processed` instead, which is how a rate-limited sweep that
 * extracted nothing still reported "N processed" as if it had succeeded.
 */
export function extractionToastOutcome(res: ExtractionToastInput): ToastOutcome {
  const failed = res.failed ?? 0;
  const extracted = res.extracted ?? 0;
  if (failed <= 0) {
    return { message: `action extraction complete · ${extracted} extracted`, warn: false };
  }
  const message = appendDominantLabel(
    `action extraction · ${extracted} extracted · ${failed} failed`,
    res.failure_categories,
  );
  return { message, warn: true };
}
