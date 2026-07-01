import type {
  BucketKey,
  Classification,
  Label,
  Overview,
  ThreadDetail,
  TriageResponse,
  User,
} from "./types";
import { mockOverview, mockThread, mockTriage, mockUser } from "./mock";

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
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
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

export async function getThread(id: string): Promise<ThreadDetail> {
  if (USE_MOCK) return mockThread(id);
  return request<ThreadDetail>(`/mail/thread/${id}`);
}

export async function getOverview(): Promise<Overview> {
  if (USE_MOCK) return mockOverview();
  return request<Overview>("/analytics/overview");
}

export async function ingestGmail(max_results = 50) {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 700));
    return { status: "ok", ingested: max_results };
  }
  return request(`/mail/ingest/gmail?max_results=${max_results}`, {
    method: "POST",
  });
}

export async function classifyBackfill(limit = 100, force = false) {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 700));
    return { status: "ok", classified: limit };
  }
  return request(
    `/mail/classify/backfill?limit=${limit}&force=${force}`,
    { method: "POST" },
  );
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
    return;
  }
  return request<{ thread_id: string; classification: Classification }>(
    `/mail/thread/${threadId}/classification`,
    { method: "POST", body: JSON.stringify({ label }) },
  );
}

export { USE_MOCK };
