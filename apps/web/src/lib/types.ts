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

export type BucketKey = Label | "all" | "unclassified";

export const BUCKETS: BucketKey[] = [
  "needs_reply",
  "action_required",
  "fyi",
  "promotional",
  "security_alert",
  "spam",
  "all",
  "unclassified",
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
  classification: Classification;
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
}

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
}

export interface ThreadDetail {
  thread: {
    id: string;
    subject: string | null;
    provider: string;
    last_message_at: string | null;
  };
  messages: ThreadMessage[];
}

export interface Overview {
  summary: { threads: number; messages: number; classified: number };
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
}
