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
  // Two different network failures, and the difference matters to whoever
  // reads this: connect_failed never reached the provider at all (so it was
  // safe to retry, and nothing was billed), while connection_failed covers
  // everything after that point -- dropped connections, but also read/write
  // timeouts and protocol errors -- so its copy claims only what's certain:
  // the request didn't complete, and it may have reached the provider.
  connect_failed: "could not reach your provider",
  connection_failed: "the request to your provider didn't complete",
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
  // Which sonner toast function the caller should fire. "warning" covers
  // every degraded-but-completed run (unchanged from phase 1); "error" is
  // for a run that STOPPED rather than degraded -- backfill's
  // llm_unavailable, and either of extraction's two early stops
  // (llm_unavailable, timed_out).
  level: "success" | "warning" | "error";
}

export interface BackfillToastInput {
  // New in phase 2 (docs/plans/2026-08-14-llm-failure-visibility-plan.md):
  // "llm_unavailable" means the run stopped early because the LLM kept
  // failing and this user has no local-model fallback configured. Absent/
  // "ok" reads as today's behaviour.
  status?: string;
  created?: number;
  fell_back?: number;
  failure_categories?: Record<string, number>;
}

/**
 * Builds the backfill result toast. `cleanMessage` is the site's own
 * unchanged success copy (the queued and inline backfill call sites use
 * slightly different wording) -- this function only decides whether that
 * copy still applies, the degraded ratio replaces it, or (phase 2) the run
 * stopped outright and this becomes an error.
 *
 * Missing fields (an older API/worker result recorded before this change)
 * all default to 0/"ok", which reads as a clean run -- exactly today's
 * behaviour.
 */
export function backfillToastOutcome(
  res: BackfillToastInput,
  cleanMessage: string,
): ToastOutcome {
  const created = res.created ?? 0;
  // D-C: no fallback configured, so the run stopped the moment the LLM
  // failed rather than keep spending a BYOK user's own quota on calls that
  // would just fail again. The partial count still says what DID land.
  if (res.status === "llm_unavailable") {
    const label = dominantFailureLabel(res.failure_categories);
    const why = label ? ` — ${label}` : "";
    const message =
      `backfill stopped early · ${created} classified so far${why} — turn on the ` +
      `built-in-model fallback in AI settings, or run backfill again once your ` +
      `provider recovers`;
    return { message, level: "error" };
  }
  const fellBack = res.fell_back ?? 0;
  if (fellBack <= 0) return { message: cleanMessage, level: "success" };
  // "N of those" rather than "N of M": fell_back is a SUBSET of what was
  // classified, and reading it as a separate total would double the apparent
  // work. A literal `${fellBack} of ${created}` would be worse -- a user
  // override landing mid-run increments fell_back but not created, so the
  // denominator can legitimately come out smaller than the numerator.
  const message = appendDominantLabel(
    `classified ${created} · ${fellBack} of those fell back to the built-in model`,
    res.failure_categories,
  );
  return { message, level: "warning" };
}

export interface ExtractionToastInput {
  status?: string;
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
  // The sweep gave up partway (phase 3): saying only what it managed to
  // attempt would hide the candidates it never reached, which is the same
  // half-truth the old "N processed" toast told.
  if (res.status === "llm_unavailable") {
    const message =
      appendDominantLabel(
        `action extraction stopped early · ${extracted} extracted so far`,
        res.failure_categories,
      ) + ` — run it again once your provider recovers`;
    return { message, level: "error" };
  }
  // The sweep hit its own time budget rather than a failing provider, so it
  // carries no diagnosis -- say what happened and nothing more. Blaming the
  // provider here would be a confident guess about a run whose calls may all
  // have SUCCEEDED; it simply ran out of time before reaching the rest.
  if (res.status === "timed_out") {
    return {
      message: `action extraction stopped early · ${extracted} extracted so far — run it again to continue`,
      level: "error",
    };
  }
  if (failed <= 0) {
    return { message: `action extraction complete · ${extracted} extracted`, level: "success" };
  }
  const message = appendDominantLabel(
    `action extraction · ${extracted} extracted · ${failed} failed`,
    res.failure_categories,
  );
  return { message, level: "warning" };
}

// Plain-words remedy for the ingest/sync toast (phase 2, same plan). Shared
// by the manual ingest button and the background auto-sync loop so the
// wording can't drift between the two call sites.
export function leftUnclassifiedMessage(count: number): string {
  return `${count} left unclassified — run backfill when your provider recovers`;
}

export interface IngestToastInput {
  left_unclassified?: number;
}

/**
 * Builds the ingest/sync result toast. `cleanMessage` is the call site's own
 * unchanged success copy -- this only decides whether it still applies or a
 * "left unclassified" warning gets appended.
 *
 * Missing/zero left_unclassified (an older API, or an account that isn't on
 * BYOK classification at all) reads as a clean run, same convention as
 * backfillToastOutcome/extractionToastOutcome above.
 */
export function ingestToastOutcome(res: IngestToastInput, cleanMessage: string): ToastOutcome {
  const left = res.left_unclassified ?? 0;
  if (left <= 0) return { message: cleanMessage, level: "success" };
  return { message: `${cleanMessage} · ${leftUnclassifiedMessage(left)}`, level: "warning" };
}
