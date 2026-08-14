export type Label =
  | "needs_reply"
  | "action_required"
  | "fyi"
  | "promotional"
  | "security_alert"
  | "spam";

export const ALL_LABELS: Label[] = [
  "needs_reply",
  "action_required",
  "fyi",
  "promotional",
  "security_alert",
  "spam",
];

export type BucketKey = Label | "all" | "unclassified" | "done";

export const BUCKETS: BucketKey[] = [
  "needs_reply",
  "action_required",
  "fyi",
  "promotional",
  "security_alert",
  "spam",
  "all",
  "unclassified",
  "done",
];

export interface Classification {
  label: Label | null;
  confidence: number | null;
  model_version: string | null;
}

export interface TriageItem {
  thread_id: string;
  subject: string | null;
  last_message_at: string | null;
  latest_message_snippet: string | null;
  latest_message_sender: string | null;
  classification: Classification;
  // Which connected Gmail account this thread belongs to.
  account_email: string;
  // The reply fence (docs/plans/2026-08-13-reply-plan.md §3.5/§3.9): null =
  // never replied from CortexMail, timestamp = when a reply attempt last
  // completed. Powers the "replied" indicator without a second call.
  replied_at: string | null;
}

export interface TriageResponse {
  bucket: BucketKey;
  items: TriageItem[];
}

export interface SearchResponse {
  query: string;
  items: TriageItem[];
}

export interface CountsResponse {
  counts: Record<BucketKey, number>;
  // The API always sends this (zeroed, not omitted, when nothing's been
  // extracted) — optional only so an older API deployment or a stale
  // cached response can still parse. Presence here says nothing about
  // whether action extraction is enabled server-side.
  actions?: ActionCounts;
}

// Mirrors ACTION_KINDS / ACTION_LABELS / ACTIONSTATUSES in the API's
// db/schemas/actions.py -- kept in sync by hand, same convention as that
// module's own comment about the extractor contract.
export type ActionKind =
  | "reply"
  | "payment"
  | "signature"
  | "form"
  | "rsvp"
  | "deadline"
  | "other";

export type ActionStatus = "open" | "done" | "dismissed";

export type DuePrecision = "date" | "datetime";

// One agenda row -- an extracted action item joined against its source
// thread/message, mirroring ActionOut in the API.
export interface ActionItem {
  id: string;
  thread_id: string;
  message_id: string;
  kind: ActionKind | null;
  title: string | null;
  due_at: string | null;
  due_precision: DuePrecision | null;
  due_raw: string | null;
  amount: number | null;
  currency: string | null;
  source_confidence: number | null;
  status: ActionStatus;
  created_at: string;
  thread_subject: string | null;
  sender: string | null;
  provider: string;
  account_email: string;
  label: string | null;
  // The source thread's latest-message timestamp. Opening a thread from the
  // agenda has to mark it seen, and the seen store compares against this --
  // without it every agenda-opened thread stays unread forever.
  last_message_at: string | null;
}

export interface ActionCounts {
  open: number;
  overdue: number;
}

export interface ActionsResponse {
  items: ActionItem[];
  counts: ActionCounts;
}

// Classifier backends an operator can pick per run. "local" = fine-tuned
// encoder, "llm" = whichever provider the routing resolves to (the user's own
// key when they've opted in, otherwise the operator's), "heuristic" = keyword
// rules. Was "gemini"; the API still accepts that spelling as an alias.
export type ClassifierBackend = "local" | "llm" | "heuristic";

export interface IngestOptions {
  maxResults: number;
  classify: boolean;
  // Re-pull threads that are already in the DB (refreshes their bodies).
  refreshExisting: boolean;
  // Which connected accounts to pull from; omitted (or every eligible
  // account checked) means all of them, same as before targeted ingest existed.
  accountIds?: string[];
}

// "recency" is the triage list's default (and only) order today; "account"
// groups threads by connected account, stable across pages, server-side.
export type TriageSort = "recency" | "account";

export interface BackfillOptions {
  limit: number;
  bucket: BucketKey;
  backend: ClassifierBackend;
  force: boolean;
}

// Small backfills run inline and report counts; over the API's inline cap the
// server queues a worker task and answers 202 with its id.
export type BackfillResult =
  | { status: "ok"; created: number; scanned: number; skipped_user_overrides: number }
  | { status: "queued"; task_id: string };

export interface ThreadMessage {
  id: string;
  sent_at: string | null;
  sender: string | null;
  snippet: string | null;
  body_text: string | null;
  body_html: string | null;
  // Client-only, never sent by the API: true for the synthetic Outlook
  // reply representation the composer renders immediately after a send
  // (docs/plans/2026-08-13-reply-plan.md §3.4) — Outlook writes no message
  // row at send time, so this stands in until the real Sent Items copy
  // syncs in and a later getThread() replaces it wholesale.
  pending?: boolean;
}

export interface ThreadDetail {
  thread: {
    id: string;
    subject: string | null;
    provider: string;
    // The provider's own thread id — powers the open-in-Gmail deep link.
    provider_thread_id: string | null;
    last_message_at: string | null;
    done: boolean;
    // Which connected Gmail account this thread belongs to.
    account_email: string;
    // See TriageItem.replied_at — same fence, also the composer's
    // expected_replied_at seed (plan §3.9).
    replied_at: string | null;
  };
  messages: ThreadMessage[];
  // The latest message's classification, so a detail pane opened from
  // somewhere other than the bucket list (the agenda) can still show the
  // prediction bar. Null when nothing has classified this thread yet.
  classification: Classification | null;
}

// The sent reply, shaped like ThreadMessage plus the recipients the send
// pipeline computed server-side (docs/plans/2026-08-13-reply-plan.md §3.4).
// For Outlook, `id` is the reply_attempt row's own id, NOT a MailMessage.id
// (§3.4/P7-3) — client-display-only, never sent back to any mutation.
export interface MessageOut {
  id: string;
  sent_at: string | null;
  sender: string | null;
  recipient: string[] | null;
  cc: string[] | null;
  snippet: string | null;
  body_text: string | null;
  body_html: string | null;
}

// POST /mail/thread/{id}/reply response (plan §4.1). `replied_at` is always
// non-null here — a successful send always advances the fence.
export interface ReplySent {
  thread_id: string;
  message: MessageOut;
  replied_at: string;
  resolved_action_items: number;
}

// POST /mail/thread/{id}/reply-draft response — stateless, BYOK-only
// (plan §3.7).
export interface ReplyDraft {
  draft_text: string;
  provider: string;
  model: string;
}

// A connected Gmail account (GET /auth/connections). `reauth_required` means
// the refresh token is dead — nothing but reconnecting fixes it.
export interface Connection {
  id: string;
  provider: string;
  email_address: string;
  created_at: string;
  reauth_required: boolean;
}

export interface Overview {
  summary: { threads: number; messages: number; classified: number };
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
}

// BYOK LLM providers, mirroring PROVIDER_PRESETS in the API's providers.py
// plus "custom". Anthropic is deliberately absent -- its OpenAI-compat layer
// ignores response_format, so it's not offered as an extraction backend.
export type LlmProvider =
  | "openai"
  | "gemini"
  | "openrouter"
  | "groq"
  | "mistral"
  | "custom";

// Mirrors LlmSettingsOut (GET/PUT /settings/llm). Unconfigured
// (configured: false) means provider/model/base_url/key_suffix/
// last_verified_at are all null -- the api_key itself never appears here.
export interface LlmSettings {
  configured: boolean;
  provider: LlmProvider | null;
  model: string | null;
  base_url: string | null;
  key_suffix: string | null;
  last_verified_at: string | null;
  extraction_enabled: boolean;
  fallback_active: boolean;
  custom_endpoints_enabled: boolean;
  private_endpoints_enabled: boolean;
  custom_blocked: boolean;
  // Per-credential opt-in: does this user's key also pay for classification
  // (every ingested message), not just extraction? False when unconfigured.
  classification_byok: boolean;
  // Whether the effective classifier backend ever reaches an LLM at all --
  // gates the opt-in checkbox itself, since a heuristic-only deployment has
  // no LLM path for the key to pay for.
  classifier_uses_llm: boolean;
  // The effective backend `classify()` dispatches on ("heuristic" | "local" |
  // "gemini" | "auto" as normalized server-side) -- lets the UI explain
  // local-first behavior instead of guessing from raw config.
  classifier_backend: string;
  // ELIGIBILITY, not observed usage -- true iff routing would actually send
  // a message to this user's key. Never renamed "active": an eligible
  // credential still isn't called when the local encoder handles a message
  // or the backend is heuristic.
  classification_eligible: boolean;
  // Would picking the "llm" backend for a run actually reach an LLM? True when
  // this user's key is eligible OR the operator has a server key. Server-derived
  // because `classification_eligible` alone can't tell "the operator pays" from
  // "this silently falls back to keyword rules".
  classification_llm_usable: boolean;
}

// POST /settings/llm/test response. `error` is one of the extractor's
// category constants (e.g. "blocked_by_policy", "invalid_response") --
// never a raw exception message.
export interface LlmTestResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

// Which background pipeline paid for a call. Kept as a plain union (not
// reusing ClassifierBackend) because it mirrors the API's CHECK-constrained
// `stage` column, not the classifier's backend selector.
export type LlmUsageStage = "classification" | "extraction" | "reply_draft";

// Shared by totals/by_stage/by_provider -- same counters, different grouping.
// `calls_with_total_tokens` can be less than `calls`: a provider can answer
// successfully without reporting `usage` at all, so this is never assumed to
// equal `calls`.
export interface LlmUsageCounters {
  calls: number;
  calls_with_total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface LlmUsageByStage extends LlmUsageCounters {
  stage: LlmUsageStage;
}

export interface LlmUsageByProvider extends LlmUsageCounters {
  provider: LlmProvider;
}

export interface LlmUsageDailyPoint {
  date: string;
  calls: number;
  total_tokens: number;
}

// GET /settings/llm/usage?days=N response -- account-level background usage
// across whatever key(s) were configured over the window, never scoped to
// "the current key" (see the plan's non-goals). A user with no usage gets
// zeroed totals and empty arrays here, never a 404.
export interface LlmUsage {
  window_days: number;
  totals: LlmUsageCounters;
  by_stage: LlmUsageByStage[];
  by_provider: LlmUsageByProvider[];
  daily: LlmUsageDailyPoint[];
}

// The closed set `Classification.model_version` maps onto, server-side (plan
// §7). `custom` deliberately has no kind of its own -- a `custom` credential
// can never route classification (presets-only), so no row can ever be
// stamped with one.
export type ClassifierMixKind =
  | "local"
  | "user_key"
  | "operator_key"
  | "heuristic"
  | "manual"
  | "unknown";

// One row of GET /settings/llm/classifier-mix -- a `kind` summed across
// every `model_version` that maps onto it, never one row per raw
// `model_version`.
export interface ClassifierMixEntry {
  kind: ClassifierMixKind;
  count: number;
}

// GET /settings/llm/classifier-mix response: which classifier produced the
// mail this user CURRENTLY has -- point-in-time state, not a time-windowed
// history (classification rows are upserted in place on reclassification
// without touching a timestamp, so a "last N days" view would silently omit
// exactly the reclassified messages that matter). Zero classifications
// yields an empty array, never a 404.
export interface ClassifierMix {
  classifier_mix: ClassifierMixEntry[];
}
