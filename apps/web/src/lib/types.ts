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
}

// Classifier backends an operator can pick per run. "local" = fine-tuned
// encoder, "gemini" = hosted LLM, "heuristic" = keyword rules.
export type ClassifierBackend = "local" | "gemini" | "heuristic";

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
  | { status: "ok"; created: number; scanned: number }
  | { status: "queued"; task_id: string };

export interface ThreadMessage {
  id: string;
  sent_at: string | null;
  sender: string | null;
  snippet: string | null;
  body_text: string | null;
  body_html: string | null;
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
  };
  messages: ThreadMessage[];
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
