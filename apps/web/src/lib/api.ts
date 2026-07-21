import type {
  BackfillOptions,
  BackfillResult,
  BucketKey,
  Classification,
  Connection,
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
  mockDeleteConnection,
  mockDeleteThread,
  mockIngest,
  mockListConnections,
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
  // The API's 500s carry an error_id that matches a server log line. Keep it so
  // a user reporting "it broke" can hand us something we can actually grep for.
  errorId?: string;
  constructor(status: number, msg: string, errorId?: string) {
    super(msg);
    this.status = status;
    this.errorId = errorId;
  }
}

// The API answers errors with {detail, error_id?}. Read it if it's there, and
// don't let a malformed body turn into a second, more confusing failure.
async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail: string | undefined;
  let errorId: string | undefined;
  try {
    const body = await res.json();
    if (body && typeof body === "object") {
      if (typeof body.detail === "string") detail = body.detail;
      if (typeof body.error_id === "string") errorId = body.error_id;
    }
  } catch {
    // No body, or not JSON -- fall back to the status line.
  }
  return new ApiError(res.status, detail ?? `${res.status} ${res.statusText}`, errorId);
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
    // A 401 means the stored token is dead -- expired, or minted before a change
    // to how we sign them. Drop it once, here, instead of leaving every caller to
    // remember to do it.
    if (res.status === 401) setToken(null);
    throw await errorFromResponse(res);
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

// Kills every token this user holds, on the server, by bumping their
// token_version. Clearing localStorage only drops *our* copy — it does nothing
// about a token someone else already walked off with.
export async function revokeAllTokens(): Promise<void> {
  if (USE_MOCK) return;
  await request<{ status: string }>("/auth/revoke-all", { method: "POST" });
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

type TokenOut = { access_token: string; token_type: string; user: User };

async function mockTokenOut(email: string): Promise<TokenOut> {
  await new Promise((resolve) => setTimeout(resolve, 120));
  return {
    access_token: "mock-token-" + Date.now(),
    token_type: "bearer",
    user: { ...mockUser(), email },
  };
}

export async function signup(
  email: string,
  password: string,
  displayName?: string,
): Promise<{ status: string }> {
  if (USE_MOCK) {
    await new Promise((resolve) => setTimeout(resolve, 120));
    return { status: "verification_sent" };
  }
  return request<{ status: string }>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({
      email,
      password,
      ...(displayName ? { display_name: displayName } : {}),
    }),
  });
}

export async function login(email: string, password: string): Promise<TokenOut> {
  if (USE_MOCK) return mockTokenOut(email);
  return request<TokenOut>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function verifyEmail(token: string): Promise<TokenOut> {
  if (USE_MOCK) return mockTokenOut(mockUser().email);
  return request<TokenOut>("/auth/verify-email", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export async function resendVerification(email: string): Promise<{ status: string }> {
  if (USE_MOCK) {
    await new Promise((resolve) => setTimeout(resolve, 120));
    return { status: "verification_sent" };
  }
  return request<{ status: string }>("/auth/resend-verification", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function forgotPassword(email: string): Promise<{ status: string }> {
  if (USE_MOCK) {
    await new Promise((resolve) => setTimeout(resolve, 120));
    return { status: "reset_sent" };
  }
  return request<{ status: string }>("/auth/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<TokenOut> {
  if (USE_MOCK) return mockTokenOut(mockUser().email);
  return request<TokenOut>("/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ token, new_password: newPassword }),
  });
}

export async function googleConnectStart(): Promise<{ auth_url: string }> {
  if (USE_MOCK) {
    throw new ApiError(0, "Google sign-in needs a live API (set VITE_API_BASE_URL)");
  }
  return request<{ auth_url: string }>("/auth/google/connect/start");
}

export async function googleConnectCallback(
  code: string,
  state: string,
): Promise<{ status: string; provider_email: string }> {
  if (USE_MOCK) {
    throw new ApiError(0, "Google sign-in needs a live API (set VITE_API_BASE_URL)");
  }
  const qs = new URLSearchParams({ code, state });
  return request<{ status: string; provider_email: string }>(
    `/auth/google/connect/callback?${qs.toString()}`,
  );
}

// Every Gmail account the operator has connected, for the accounts menu.
export async function listConnections(): Promise<Connection[]> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 100));
    return mockListConnections();
  }
  const res = await request<{ connections: Connection[] }>("/auth/connections");
  return res.connections;
}

// Disconnects a Gmail account and deletes everything it synced (server-side
// cascade). 404s if the id isn't a live connection of this user's.
export async function deleteConnection(id: string): Promise<void> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 120));
    if (!mockDeleteConnection(id)) throw new ApiError(404, "Not Found");
    return;
  }
  await request<void>(`/auth/connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
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
    `/mail/thread/${encodeURIComponent(threadId)}/done`,
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

// Fire-and-forget delete for when the page is going away while a delete is
// still inside its undo window — keepalive lets the request outlive the
// document, so a reload/close doesn't silently drop the deletion.
export function flushDeleteThread(threadId: string): void {
  if (USE_MOCK) {
    mockDeleteThread(threadId);
    return;
  }
  const token = getToken();
  void fetch(`${BASE}/mail/thread/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
    keepalive: true,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).catch(() => {
    /* page is unloading; nothing left to report to */
  });
}

export async function getOverview(): Promise<Overview> {
  if (USE_MOCK) return mockOverview();
  return request<Overview>("/analytics/overview");
}

// Queues a Gmail pull on the worker, one run per connected non-paused
// account. `max_results` is a THREAD count now, so N gives you N threads (and
// all their messages) PER ACCOUNT, not N messages. With `refreshExisting` the
// pull re-fetches threads already in the DB (the upsert refreshes their
// bodies) instead of skipping ahead to new ones. `newOnly` (auto-sync) pulls
// just mail newer than the newest known thread per account — no backfill of
// older history. The real API returns 202 with one run per account (empty
// when nothing's connected); the mock resolves as if already done.
export async function ingestGmail(
  max_results = 50,
  classify = true,
  refreshExisting = false,
  newOnly = false,
): Promise<SyncRunStatus[]> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 150));
    const newThreads = mockIngest();
    // The mock "inbox" actually receives mail on each pull, so auto-sync and
    // the new-mail pill are demoable (and testable) offline. All of it lands
    // attributed to the first mock account; the rest report an empty, still-
    // successful run, same as a real account with nothing new to pull.
    return mockListConnections().map((acct, i) => ({
      run_id: `mock-run-${acct.id}`,
      task_id: undefined,
      mode: newOnly ? "auto" : "manual",
      status: "succeeded",
      ready: true,
      deduplicated: false,
      provider_account_id: acct.id,
      result:
        i === 0
          ? {
              status: "ok",
              threads_upserted: newThreads,
              messages_upserted: newThreads * 3,
              new_threads: newThreads,
            }
          : { status: "ok", threads_upserted: 0, messages_upserted: 0, new_threads: 0 },
    }));
  }
  const res = await request<{ runs: SyncRunStatus[] }>(
    `/mail/ingest/gmail?max_results=${max_results}&classify=${classify}` +
      `&skip_existing=${!refreshExisting}&new_only=${newOnly}`,
    { method: "POST" },
  );
  return res.runs;
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
  threads_reopened?: number;
  fetched?: number;
  skipped_existing?: number;
  // Mail that actually arrived since the last pull; threads_upserted also
  // counts older history the deduping ingest backfilled.
  new_threads?: number;
  created?: number;
  scanned?: number;
  processed?: number;
}

export interface SyncRunStatus {
  run_id: string;
  task_id?: string;
  mode: string;
  status: string;
  ready: boolean;
  deduplicated: boolean;
  result?: TaskResult | null;
  error?: string | null;
  // Which Gmail account this run is pulling from. Null on legacy rows a
  // migration couldn't attribute to a single account.
  provider_account_id: string | null;
}

// Multi-account fan-out: one run per connected account, [] when idle/none.
export async function getActiveSync(signal?: AbortSignal): Promise<SyncRunStatus[]> {
  if (USE_MOCK) return [];
  const res = await request<{ runs: SyncRunStatus[] }>("/mail/sync/active", { signal });
  return res.runs;
}

export interface AccountSyncHealth {
  provider_account_id: string;
  email_address: string;
  last_succeeded_at: string | null;
  stale: boolean;
  sync_in_progress: boolean;
  // null | never_synced | reauth_required | <sync_pause_reason> — the pause
  // reason is whatever the server recorded when it paused the account, so
  // this stays open rather than pinned to a fixed union.
  reason: string | null;
}

export interface SyncHealth {
  // Worst-of aggregate across every connected account, so the console's
  // existing pill logic keeps working unchanged.
  last_succeeded_at: string | null;
  // Is the mail itself behind?
  stale: boolean;
  sync_in_progress: boolean;
  // Check if the server-side scheduler is still checking in. Separate from `stale` on
  // purpose since the browser fallback can keep mail flowing while the scheduler is
  // dead.
  scheduler_alive: boolean;
  threshold_seconds: number;
  reason: "never_synced" | "reauth_required" | "not_connected" | null;
  // Per-account breakdown of the same fields, for the accounts menu.
  accounts: AccountSyncHealth[];
}

export async function getSyncHealth(signal?: AbortSignal): Promise<SyncHealth> {
  if (USE_MOCK) {
    const accounts = mockListConnections();
    const now = new Date().toISOString();
    return {
      last_succeeded_at: accounts.length ? now : null,
      stale: false,
      sync_in_progress: false,
      scheduler_alive: true,
      threshold_seconds: 1800,
      reason: accounts.length ? null : "not_connected",
      accounts: accounts.map((a) => ({
        provider_account_id: a.id,
        email_address: a.email_address,
        last_succeeded_at: now,
        stale: false,
        sync_in_progress: false,
        reason: null,
      })),
    };
  }
  return request<SyncHealth>("/mail/sync/health", { signal });
}

// Does every run in this ingest batch represent a sync some other caller
// already started? If so the batch didn't do fresh work — the UI should say
// "waiting for it", not "ingest complete".
export function allRunsDeduplicated(runs: SyncRunStatus[]): boolean {
  return runs.length > 0 && runs.every((r) => r.deduplicated);
}

// Sums a finished batch's per-account upsert counts into the one aggregate
// toast a multi-account ingest gets.
export function sumIngestResults(finals: SyncRunStatus[]): {
  threads: number;
  messages: number;
} {
  return finals.reduce(
    (acc, f) => ({
      threads: acc.threads + (f.result?.threads_upserted ?? f.result?.new_threads ?? 0),
      messages: acc.messages + (f.result?.messages_upserted ?? 0),
    }),
    { threads: 0, messages: 0 },
  );
}

export async function getSyncRun(
  runId: string,
  signal?: AbortSignal,
): Promise<SyncRunStatus> {
  if (USE_MOCK) {
    return {
      run_id: runId,
      mode: "auto",
      status: "succeeded",
      ready: true,
      deduplicated: false,
      provider_account_id: null,
      result: { status: "ok" },
    };
  }
  return request<SyncRunStatus>(`/mail/sync/${encodeURIComponent(runId)}`, { signal });
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function waitForSyncRun(
  runId: string,
  {
    signal,
    timeoutMs = WORKER_TASK_TIMEOUT_MS,
  }: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<SyncRunStatus> {
  const started = Date.now();
  for (;;) {
    const run = await getSyncRun(runId, signal);
    if (run.ready) return run;
    if (Date.now() - started >= timeoutMs) return run;
    await abortableDelay(Date.now() - started < 60_000 ? 2000 : 10_000, signal);
  }
}

// A multi-account ingest queues one run per account; this waits out every
// run in parallel (already-ready ones resolve immediately) instead of
// serially polling each account's task one after another.
export function waitForSyncRuns(
  runs: SyncRunStatus[],
  opts?: { signal?: AbortSignal; timeoutMs?: number },
): Promise<PromiseSettledResult<SyncRunStatus>[]> {
  return Promise.allSettled(
    runs.map((r) => (r.ready ? Promise.resolve(r) : waitForSyncRun(r.run_id, opts))),
  );
}

export interface TaskStatus {
  task_id: string;
  state: string;
  ready: boolean;
  result?: TaskResult | null;
  error?: string;
}

// Ingest workers have a 30-minute hard limit. Poll beyond that boundary so a
// slow but healthy task still produces one authoritative UI refresh instead
// of silently finishing after the browser gave up at 90/120 seconds.
export const WORKER_TASK_TIMEOUT_MS = 35 * 60 * 1000;

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
  {
    intervalMs = 1500,
    timeoutMs = 120_000,
    signal,
  }: { intervalMs?: number; timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<TaskStatus> {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  for (;;) {
    const status = await getTaskStatus(taskId);
    if (status.ready) return status;
    if (Date.now() - start >= timeoutMs) return status;
    await abortableDelay(intervalMs, signal);
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
