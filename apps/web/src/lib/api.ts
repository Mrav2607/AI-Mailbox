import type {
  BackfillOptions,
  BackfillResult,
  BucketKey,
  Classification,
  CountsResponse,
  Label,
  Overview,
  SearchResponse,
  ThreadDetail,
  TriageResponse,
  User,
} from "./types";
import {
  mockApplyLabel,
  mockBackfill,
  mockCounts,
  mockDeleteThread,
  mockIngest,
  mockOverview,
  mockSearch,
  mockSetDone,
  mockThread,
  mockTriage,
  mockUser,
} from "./mock";

const TOKEN_KEY = "ai_mailbox_token";
const BASE = (import.meta.env.VITE_API_BASE_URL as string) ?? "";
const USE_MOCK = !BASE;

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string | null) {
  if (typeof window === "undefined") return;
  if (t) window.localStorage.setItem(TOKEN_KEY, t);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, msg: string) {
    super(msg);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  if (USE_MOCK) throw new Error("no-base"); // never reached — callers branch on USE_MOCK
  const token = getToken();
  const res = await fetch(`${BASE}${path}`, {
    ...opts,
    headers: {
      // Only declare a Content-Type when there's a body: it's not a
      // CORS-safelisted header, so putting it on GETs forces a preflight
      // round-trip on every cross-origin read.
      ...(opts.body != null ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
  // 204 No Content (e.g. DELETE) has no body to parse.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function getMe(): Promise<User> {
  if (USE_MOCK) {
    if (!getToken()) throw new ApiError(401, "no token");
    return mockUser();
  }
  return request<User>("/auth/me");
}

// --- Google OAuth -----------------------------------------------------------
// The backend owns the OAuth exchange. `start` returns the Google consent URL
// to redirect the browser to; Google then redirects back to GOOGLE_REDIRECT_URI
// (the SPA's /auth/google/callback path) with a `code`, which the SPA hands to
// `callback` below to mint a session token. No client secret touches the front
// end. Both calls are unauthenticated (this is the login path).
export async function googleAuthStart(): Promise<{ auth_url: string }> {
  if (USE_MOCK) {
    throw new ApiError(0, "Google sign-in needs a live API (set VITE_API_BASE_URL)");
  }
  return request<{ auth_url: string }>("/auth/google/start");
}

export async function googleAuthCallback(
  code: string,
  state?: string | null,
): Promise<{ access_token: string; token_type: string; user: User }> {
  if (USE_MOCK) {
    // Same story as googleAuthStart: nobody should land here in preview mode,
    // but if they paste the callback URL, fail with the friendly error.
    throw new ApiError(0, "Google sign-in needs a live API (set VITE_API_BASE_URL)");
  }
  const qs = new URLSearchParams({ code });
  if (state) qs.set("state", state);
  return request<{ access_token: string; token_type: string; user: User }>(
    `/auth/google/callback?${qs.toString()}`,
  );
}

export async function demoLogin(
  email: string,
): Promise<{ access_token: string; token_type: string; user: User }> {
  if (USE_MOCK) {
    return {
      access_token: "mock-token-" + Date.now(),
      token_type: "bearer",
      user: { ...mockUser(), email },
    };
  }
  return request("/auth/demo-login", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function getTriage(
  bucket: BucketKey,
  limit = 100,
): Promise<TriageResponse> {
  if (USE_MOCK) return mockTriage(bucket, limit);
  return request<TriageResponse>(
    `/mail/triage?bucket=${encodeURIComponent(bucket)}&limit=${limit}`,
  );
}

// Whole-mailbox thread counts per bucket for the sidebar. Computed server-side
// so the totals aren't capped by a single triage page.
export async function getCounts(): Promise<Record<BucketKey, number>> {
  if (USE_MOCK) return mockCounts();
  const res = await request<CountsResponse>("/mail/counts");
  return res.counts;
}

export async function getThread(id: string): Promise<ThreadDetail> {
  if (USE_MOCK) return mockThread(id);
  return request<ThreadDetail>(`/mail/thread/${id}`);
}

// Cross-bucket search over the whole mailbox (subject / sender / snippet).
export async function searchThreads(
  q: string,
  limit = 50,
): Promise<SearchResponse> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 120));
    return mockSearch(q, limit);
  }
  return request<SearchResponse>(
    `/mail/search?q=${encodeURIComponent(q)}&limit=${limit}`,
  );
}

// Mark a thread done (clears it from the open triage buckets, keeps it
// searchable and in the `done` bucket) or restore it with done=false.
export async function setThreadDone(
  threadId: string,
  done: boolean,
): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 120));
    mockSetDone(threadId, done);
    return;
  }
  await request<{ thread_id: string; done: boolean }>(
    `/mail/thread/${threadId}/done`,
    { method: "POST", body: JSON.stringify({ done }) },
  );
}

// Permanently delete a thread (and its messages/classifications via cascade).
export async function deleteThread(threadId: string): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 120));
    mockDeleteThread(threadId);
    return;
  }
  await request<void>(`/mail/thread/${threadId}`, { method: "DELETE" });
}

export async function getOverview(): Promise<Overview> {
  if (USE_MOCK) return mockOverview();
  return request<Overview>("/analytics/overview");
}

// Queues a Gmail pull on the worker. `max_results` is a THREAD count now, so N
// gives you N threads (and all their messages), not N messages. With
// `refreshExisting` the pull re-fetches threads already in the DB (the upsert
// refreshes their bodies) instead of skipping ahead to new ones. The real API
// returns a task_id to poll; the mock resolves as if already done.
export async function ingestGmail(
  max_results = 50,
  classify = true,
  refreshExisting = false,
): Promise<{ status: string; task_id?: string; new_threads?: number }> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    // The mock "inbox" actually receives mail on each pull, so auto-sync and
    // the new-mail pill are demoable (and testable) offline.
    return { status: "ok", new_threads: mockIngest() };
  }
  return request<{ status: string; task_id?: string }>(
    `/mail/ingest/gmail?max_results=${max_results}&classify=${classify}` +
      `&skip_existing=${!refreshExisting}`,
    { method: "POST" },
  );
}

// Result payload a worker task reports back through the status endpoint.
// Ingest reports upsert counts, backfill reports created/scanned, and the
// classify queue reports created/processed; `status: "error"` is a user-facing
// problem (e.g. Gmail not connected) that still comes back as a *completed* task.
export interface TaskResult {
  status?: string;
  detail?: string;
  threads_upserted?: number;
  messages_upserted?: number;
  classified?: number;
  fetched?: number;
  skipped_existing?: number;
  // Mail that actually arrived since the last pull; threads_upserted also
  // counts older history the deduping ingest backfilled.
  new_threads?: number;
  created?: number;
  scanned?: number;
  processed?: number;
}

export interface TaskStatus {
  task_id: string;
  state: string;
  ready: boolean;
  result?: TaskResult | null;
  error?: string;
}

export async function getTaskStatus(taskId: string): Promise<TaskStatus> {
  if (USE_MOCK) {
    // Mock "workers" finish instantly, so any polled task is already done.
    await new Promise((r) => setTimeout(r, 300));
    return { task_id: taskId, state: "SUCCESS", ready: true, result: { status: "ok" } };
  }
  return request<TaskStatus>(`/mail/tasks/${encodeURIComponent(taskId)}`);
}

// Poll a queued task until it finishes. Returns the final status; if it's still
// running when the timeout hits, returns the last (not-ready) status so the
// caller can decide what to do rather than hanging forever.
export async function waitForTask(
  taskId: string,
  { intervalMs = 1500, timeoutMs = 120_000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<TaskStatus> {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  for (;;) {
    const status = await getTaskStatus(taskId);
    if (status.ready) return status;
    if (Date.now() - start >= timeoutMs) return status;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export async function classifyBackfill(
  opts: BackfillOptions,
): Promise<BackfillResult> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 700));
    return mockBackfill(opts);
  }
  const qs = new URLSearchParams({
    limit: String(opts.limit),
    force: String(opts.force),
    bucket: opts.bucket,
    backend: opts.backend,
  });
  return request<BackfillResult>(`/mail/classify/backfill?${qs.toString()}`, {
    method: "POST",
  });
}

export async function classifyQueue(limit = 100, force = false) {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 400));
    return { status: "queued", task_id: "task_" + Date.now() };
  }
  return request<{ status: string; task_id: string }>(
    `/mail/classify/queue?limit=${limit}&force=${force}`,
    { method: "POST" },
  );
}

// Apply an operator's manual label to a thread. The UI updates optimistically
// (see App.doReclassify); a rejected promise here lets it surface the failure.
export async function reclassify(
  threadId: string,
  label: Label,
): Promise<{ thread_id: string; classification: Classification } | void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    // Mutate the mock store so the label survives re-fetches, same as the
    // other mock write paths (delete/backfill) do.
    mockApplyLabel(threadId, label);
    return;
  }
  return request<{ thread_id: string; classification: Classification }>(
    `/mail/thread/${threadId}/classification`,
    { method: "POST", body: JSON.stringify({ label }) },
  );
}

export { USE_MOCK };
