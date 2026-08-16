import { afterEach, describe, expect, it, vi } from "vitest";

import {
  allRunsDeduplicated,
  buildSearchQuery,
  buildTriageQuery,
  sumIngestResults,
  waitForSyncRuns,
  type ApiError,
  type SyncRunStatus,
} from "./api";

// Everything below this point in the file needs USE_MOCK=false (the live
// fetch branch) to exercise the request paths, but api.ts decides that once,
// from VITE_API_BASE_URL, at module load time. A dev .env sets that var
// locally but CI has none, so relying on the ambient env would make these
// tests pass or fail depending on who's running them. Stubbing the env and
// re-importing the module (vi.resetModules forces a fresh transform, which
// re-reads import.meta.env) pins USE_MOCK=false everywhere, regardless.
async function importLiveApi() {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8000/api/v1");
  return import("./api");
}

// The mirror of importLiveApi: forces USE_MOCK=true regardless of the dev
// .env this repo ships (see the comment above), so mock-branch assertions
// don't depend on who's running the suite either.
async function importMockApi() {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", "");
  return import("./api");
}

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof vi.fn>): URL {
  return new URL(fetchMock.mock.calls[0][0] as string);
}

function run(patch: Partial<SyncRunStatus>): SyncRunStatus {
  return {
    run_id: "run-1",
    mode: "manual",
    status: "succeeded",
    ready: true,
    deduplicated: false,
    provider_account_id: null,
    result: null,
    ...patch,
  };
}

describe("allRunsDeduplicated", () => {
  it("is false for an empty batch (nothing connected, not 'already running')", () => {
    expect(allRunsDeduplicated([])).toBe(false);
  });

  it("is false when only some accounts' runs deduplicated", () => {
    const runs = [run({ deduplicated: true }), run({ deduplicated: false })];
    expect(allRunsDeduplicated(runs)).toBe(false);
  });

  it("is true only once every account's run deduplicated", () => {
    const runs = [
      run({ run_id: "a", deduplicated: true }),
      run({ run_id: "b", deduplicated: true }),
    ];
    expect(allRunsDeduplicated(runs)).toBe(true);
  });
});

describe("sumIngestResults", () => {
  it("sums threads and messages across every account's run", () => {
    const finals = [
      run({ result: { status: "ok", threads_upserted: 3, messages_upserted: 9 } }),
      run({ result: { status: "ok", threads_upserted: 1, messages_upserted: 2 } }),
    ];
    expect(sumIngestResults(finals)).toEqual({ threads: 4, messages: 11, leftUnclassified: 0 });
  });

  it("falls back to new_threads when threads_upserted is missing", () => {
    const finals = [run({ result: { status: "ok", new_threads: 2 } })];
    expect(sumIngestResults(finals)).toEqual({ threads: 2, messages: 0, leftUnclassified: 0 });
  });

  it("treats a run with no result as contributing nothing", () => {
    expect(sumIngestResults([run({ result: null })])).toEqual({
      threads: 0,
      messages: 0,
      leftUnclassified: 0,
    });
  });

  it("sums left_unclassified across every account's run", () => {
    const finals = [
      run({ result: { status: "ok", left_unclassified: 3 } }),
      run({ result: { status: "ok", left_unclassified: 5 } }),
      // Older/BYOK-off runs never send this field -- must not throw.
      run({ result: { status: "ok" } }),
    ];
    expect(sumIngestResults(finals)).toEqual({ threads: 0, messages: 0, leftUnclassified: 8 });
  });
});

describe("buildTriageQuery", () => {
  it("omits offset, sort, and account when they're at their defaults", () => {
    expect(buildTriageQuery("needs_reply", 200)).toBe("bucket=needs_reply&limit=200");
    expect(
      buildTriageQuery("needs_reply", 200, { offset: 0, sort: "recency", accountId: null }),
    ).toBe("bucket=needs_reply&limit=200");
  });

  it("includes offset once it's past the first page", () => {
    expect(buildTriageQuery("all", 200, { offset: 200 })).toBe(
      "bucket=all&limit=200&offset=200",
    );
  });

  it("includes sort only when it's not the default recency", () => {
    expect(buildTriageQuery("all", 200, { sort: "account" })).toBe(
      "bucket=all&limit=200&sort=account",
    );
  });

  it("includes provider_account_id only when an account is set", () => {
    expect(buildTriageQuery("all", 200, { accountId: "acct-1" })).toBe(
      "bucket=all&limit=200&provider_account_id=acct-1",
    );
  });

  it("combines every non-default param", () => {
    expect(
      buildTriageQuery("all", 200, { offset: 400, sort: "account", accountId: "acct-1" }),
    ).toBe("bucket=all&limit=200&offset=400&sort=account&provider_account_id=acct-1");
  });
});

describe("buildSearchQuery", () => {
  it("omits provider_account_id when unset", () => {
    expect(buildSearchQuery("invoice", 200)).toBe("q=invoice&limit=200");
  });

  it("includes provider_account_id when an account is set", () => {
    expect(buildSearchQuery("invoice", 200, "acct-1")).toBe(
      "q=invoice&limit=200&provider_account_id=acct-1",
    );
  });
});

describe("waitForSyncRuns", () => {
  it("resolves already-ready runs immediately, without polling", async () => {
    const runs = [run({ run_id: "a" }), run({ run_id: "b" })];
    const settled = await waitForSyncRuns(runs);
    expect(settled).toHaveLength(2);
    expect(settled.every((s) => s.status === "fulfilled")).toBe(true);
    const values = settled.map((s) => (s as PromiseFulfilledResult<SyncRunStatus>).value);
    expect(values.map((v) => v.run_id)).toEqual(["a", "b"]);
  });
});

describe("microsoft oauth", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("microsoftAuthStart requests /auth/microsoft/start", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ auth_url: "https://login.microsoftonline.com/consent" });
    const res = await api.microsoftAuthStart();
    expect(res.auth_url).toBe("https://login.microsoftonline.com/consent");
    expect(requestedUrl(fetchMock).pathname).toBe("/api/v1/auth/microsoft/start");
  });

  it("microsoftAuthCallback sends code and state to /auth/microsoft/callback", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      access_token: "tok",
      token_type: "bearer",
      user: { id: "u1", email: "a@b.com" },
    });
    await api.microsoftAuthCallback("code-1", "state-1");
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/auth/microsoft/callback");
    expect(url.searchParams.get("code")).toBe("code-1");
    expect(url.searchParams.get("state")).toBe("state-1");
  });

  it("microsoftAuthCallback omits state when it isn't given", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      access_token: "tok",
      token_type: "bearer",
      user: { id: "u1", email: "a@b.com" },
    });
    await api.microsoftAuthCallback("code-1");
    expect(requestedUrl(fetchMock).searchParams.has("state")).toBe(false);
  });

  it("microsoftConnectStart requests /auth/microsoft/connect/start", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ auth_url: "https://login.microsoftonline.com/connect" });
    await api.microsoftConnectStart();
    expect(requestedUrl(fetchMock).pathname).toBe("/api/v1/auth/microsoft/connect/start");
  });

  it("microsoftConnectCallback sends code and state to /auth/microsoft/connect/callback", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ status: "connected", provider_email: "a@b.com" });
    await api.microsoftConnectCallback("code-1", "state-1");
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/auth/microsoft/connect/callback");
    expect(url.searchParams.get("code")).toBe("code-1");
    expect(url.searchParams.get("state")).toBe("state-1");
  });
});

describe("listAuthProviders", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("requests /auth/providers and returns the provider list", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ providers: ["gmail", "outlook"] });
    const providers = await api.listAuthProviders();
    expect(providers).toEqual(["gmail", "outlook"]);
    expect(requestedUrl(fetchMock).pathname).toBe("/api/v1/auth/providers");
  });

  it("reports the demo-login flag alongside the providers", async () => {
    const api = await importLiveApi();
    stubFetch({ providers: ["gmail"], demo_login: true });
    await expect(api.listAuthOptions()).resolves.toEqual({
      providers: ["gmail"],
      demoLogin: true,
    });
  });

  // An older API that predates the flag must not light up a control it can't
  // serve, so a missing field reads as "no demo login", not undefined.
  it("treats a missing demo_login as disabled", async () => {
    const api = await importLiveApi();
    stubFetch({ providers: ["gmail"] });
    await expect(api.listAuthOptions()).resolves.toEqual({
      providers: ["gmail"],
      demoLogin: false,
    });
  });
});

describe("ingestMail", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts to the renamed /mail/ingest route (not the old /mail/ingest/gmail)", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ runs: [] });
    await api.ingestMail();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/ingest");
    expect((opts as RequestInit).method).toBe("POST");
  });
});

describe("getCounts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("returns the FULL counts payload -- no longer unwraps to just res.counts", async () => {
    const api = await importLiveApi();
    const body = {
      counts: { all: 5, needs_reply: 2 },
      actions: { open: 3, overdue: 1 },
    };
    stubFetch(body);
    const res = await api.getCounts();
    expect(res).toEqual(body);
  });

  it("omits provider_account_id when no account is given, includes it otherwise", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ counts: {}, actions: { open: 0, overdue: 0 } });
    await api.getCounts();
    expect(requestedUrl(fetchMock).pathname).toBe("/api/v1/mail/counts");

    const fetchMock2 = stubFetch({ counts: {}, actions: { open: 0, overdue: 0 } });
    await api.getCounts("acct-1");
    const url2 = requestedUrl(fetchMock2);
    expect(url2.pathname).toBe("/api/v1/mail/counts");
    expect(url2.searchParams.get("provider_account_id")).toBe("acct-1");
  });

  it("in mock mode, returns bucket counts plus the cross-account actions tally", async () => {
    const api = await importMockApi();
    const res = await api.getCounts();
    expect(res.counts.all).toBeGreaterThan(0);
    expect(res.actions).toBeDefined();
    expect(res.actions!.open).toBeGreaterThan(0);
  });
});

describe("getActions", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("requests /mail/actions with status and limit, defaulting to open/200", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ items: [], counts: { open: 0, overdue: 0 } });
    await api.getActions();
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/mail/actions");
    expect(url.searchParams.get("status")).toBe("open");
    expect(url.searchParams.get("limit")).toBe("200");
  });

  it("passes a non-default status and limit through", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ items: [], counts: { open: 0, overdue: 0 } });
    await api.getActions("dismissed", 50);
    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get("status")).toBe("dismissed");
    expect(url.searchParams.get("limit")).toBe("50");
  });

  it("returns the items+counts payload untouched", async () => {
    const api = await importLiveApi();
    const body = { items: [{ id: "a1" }], counts: { open: 1, overdue: 0 } };
    stubFetch(body);
    const res = await api.getActions();
    expect(res).toEqual(body);
  });

  it("in mock mode, serves the demo agenda board", async () => {
    const api = await importMockApi();
    const res = await api.getActions();
    expect(res.items.length).toBeGreaterThan(0);
    expect(res.items.every((i) => i.status === "open")).toBe(true);
  });
});

describe("setActionStatus", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts the new status to /mail/actions/{id}/status", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ action_id: "a1", status: "done", status_at: "2026-01-01T00:00:00Z" });
    const res = await api.setActionStatus("a1", "done");
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/actions/a1/status");
    expect((opts as RequestInit).method).toBe("POST");
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({ status: "done" });
    expect(res.status).toBe("done");
  });

  it("URL-encodes the action id", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ action_id: "a/1", status: "open", status_at: null });
    await api.setActionStatus("a/1", "open");
    expect(new URL(fetchMock.mock.calls[0][0] as string).pathname).toBe(
      "/api/v1/mail/actions/a%2F1/status",
    );
  });

  it("in mock mode, round-trips a status change against the demo board", async () => {
    const api = await importMockApi();
    const { items } = await api.getActions();
    const res = await api.setActionStatus(items[0].id, "dismissed");
    expect(res.status).toBe("dismissed");
    expect(res.status_at).not.toBeNull();
  });
});

describe("snoozeThread", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts { until } to /mail/thread/{id}/snooze", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      thread_id: "t1",
      snoozed_until: "2026-08-15T08:00:00Z",
      snoozed_at: "2026-08-14T10:00:00Z",
    });
    const res = await api.snoozeThread("t1", "2026-08-15T08:00:00Z");
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/thread/t1/snooze");
    expect((opts as RequestInit).method).toBe("POST");
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({
      until: "2026-08-15T08:00:00Z",
    });
    expect(res.snoozed_until).toBe("2026-08-15T08:00:00Z");
  });

  it("posts { until: null } to clear an existing snooze", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ thread_id: "t1", snoozed_until: null, snoozed_at: null });
    const res = await api.snoozeThread("t1", null);
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      until: null,
    });
    expect(res.snoozed_until).toBeNull();
    expect(res.snoozed_at).toBeNull();
  });

  it("URL-encodes the thread id", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ thread_id: "t/1", snoozed_until: null, snoozed_at: null });
    await api.snoozeThread("t/1", null);
    expect(new URL(fetchMock.mock.calls[0][0] as string).pathname).toBe(
      "/api/v1/mail/thread/t%2F1/snooze",
    );
  });

  it("surfaces the server's structured snooze_too_soon code", async () => {
    const api = await importLiveApi();
    stubFetch(
      { detail: { code: "snooze_too_soon", message: "until must be more than 60s in the future" } },
      422,
    );
    await expect(api.snoozeThread("t1", "2026-08-14T10:00:30Z")).rejects.toMatchObject({
      status: 422,
      code: "snooze_too_soon",
    });
  });

  it("in mock mode, round-trips a snooze/unsnooze against the demo mailbox", async () => {
    const api = await importMockApi();
    const { items } = await api.getTriage("all", 1);
    const id = items[0].thread_id;
    const until = new Date(Date.now() + 3 * 60 * 60 * 1000).toISOString();

    const res = await api.snoozeThread(id, until);
    expect(res.snoozed_until).toBe(until);

    const cleared = await api.snoozeThread(id, null);
    expect(cleared.snoozed_until).toBeNull();
  });
});

describe("updateConnection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("PATCHes { label_sync_enabled } to /auth/connections/{id}", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      id: "c1",
      provider: "gmail",
      created_at: "2026-08-01T00:00:00Z",
      email_address: "operator@gmail.com",
      reauth_required: false,
      label_sync_enabled: true,
      label_sync_drift: 12,
    });
    const res = await api.updateConnection("c1", { label_sync_enabled: true });
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/auth/connections/c1");
    expect((opts as RequestInit).method).toBe("PATCH");
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({
      label_sync_enabled: true,
    });
    expect(res.label_sync_enabled).toBe(true);
    expect(res.label_sync_drift).toBe(12);
  });

  it("URL-encodes the connection id", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      id: "c/1",
      provider: "gmail",
      created_at: "2026-08-01T00:00:00Z",
      email_address: "operator@gmail.com",
      reauth_required: false,
      label_sync_enabled: false,
      label_sync_drift: null,
    });
    await api.updateConnection("c/1", { label_sync_enabled: false });
    expect(new URL(fetchMock.mock.calls[0][0] as string).pathname).toBe(
      "/api/v1/auth/connections/c%2F1",
    );
  });

  // Every ENABLE-time failure code (plan §3.3) rides the same structured
  // {code, message} envelope the reply-send route pioneered -- one
  // representative case here proves errorFromResponse parses it onto
  // ApiError.code the same way for this route too.
  it("surfaces the server's structured label_sync_busy code", async () => {
    const api = await importLiveApi();
    stubFetch(
      {
        detail: {
          code: "label_sync_busy",
          message: "a sync is finishing — try again in a few minutes",
        },
      },
      409,
    );
    await expect(
      api.updateConnection("c1", { label_sync_enabled: true }),
    ).rejects.toMatchObject({ status: 409, code: "label_sync_busy" });
  });

  it("in mock mode, round-trips enabling and disabling label sync for a connection", async () => {
    const api = await importMockApi();
    const [conn] = await api.listConnections();
    expect(conn.label_sync_enabled).toBe(false);
    expect(conn.label_sync_drift).toBeNull();

    const enabled = await api.updateConnection(conn.id, { label_sync_enabled: true });
    expect(enabled.label_sync_enabled).toBe(true);
    expect(enabled.label_sync_drift).toEqual(expect.any(Number));

    const disabled = await api.updateConnection(conn.id, { label_sync_enabled: false });
    expect(disabled.label_sync_enabled).toBe(false);
    expect(disabled.label_sync_drift).toBeNull();
  });
});

describe("backfillActions", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts to /mail/actions/backfill with default limit/force/since_days", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ status: "queued", task_id: "task_1" });
    const res = await api.backfillActions();
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/mail/actions/backfill");
    expect(url.searchParams.get("limit")).toBe("100");
    expect(url.searchParams.get("force")).toBe("false");
    expect(url.searchParams.get("since_days")).toBe("30");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(res).toEqual({ status: "queued", task_id: "task_1" });
  });

  it("passes non-default opts through as query params", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ status: "queued", task_id: "task_2" });
    await api.backfillActions({ limit: 10, force: true, since_days: 7 });
    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("force")).toBe("true");
    expect(url.searchParams.get("since_days")).toBe("7");
  });

  it("in mock mode, resolves a queued mock task", async () => {
    const api = await importMockApi();
    const res = await api.backfillActions();
    expect(res.status).toBe("queued");
    expect(res.task_id).toMatch(/^mock-actions-task-/);
  });
});

describe("getLlmSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("requests GET /settings/llm", async () => {
    const api = await importLiveApi();
    const body = {
      configured: true,
      provider: "openai",
      model: "gpt-4o-mini",
      base_url: "https://api.openai.com/v1",
      key_suffix: "ab12",
      last_verified_at: null,
      extraction_enabled: true,
      fallback_active: false,
      custom_endpoints_enabled: false,
      private_endpoints_enabled: false,
      custom_blocked: false,
    };
    const fetchMock = stubFetch(body);
    const res = await api.getLlmSettings();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/settings/llm");
    expect((opts as RequestInit).method ?? "GET").toBe("GET");
    expect(res).toEqual(body);
  });

  it("in mock mode, returns the demo settings state", async () => {
    const api = await importMockApi();
    const res = await api.getLlmSettings();
    expect(res.configured).toBe(true);
    expect(res.provider).toBe("openai");
  });
});

describe("putLlmSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("PUTs to /settings/llm with the submitted fields as the body", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      configured: true,
      provider: "groq",
      model: "llama-3.1-8b-instant",
      base_url: "https://api.groq.com/openai/v1",
      key_suffix: "cdef",
      last_verified_at: null,
      extraction_enabled: true,
      fallback_active: false,
      custom_endpoints_enabled: false,
      private_endpoints_enabled: false,
      custom_blocked: false,
    });
    const input = {
      provider: "groq" as const,
      api_key: "gsk_live_abcdef",
      model: "llama-3.1-8b-instant",
    };
    const res = await api.putLlmSettings(input);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/settings/llm");
    expect((opts as RequestInit).method).toBe("PUT");
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual(input);
    expect(res.provider).toBe("groq");
  });

  it("in mock mode, updates the demo settings and derives key_suffix", async () => {
    const api = await importMockApi();
    const res = await api.putLlmSettings({
      provider: "mistral",
      api_key: "mistral-key-7890",
      model: "mistral-small",
    });
    expect(res.provider).toBe("mistral");
    expect(res.key_suffix).toBe("7890");
    expect(res.last_verified_at).toBeNull();
  });

  it("passes classification_byok through to the request body when given", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      configured: true,
      provider: "groq",
      model: "llama-3.1-8b-instant",
      base_url: "https://api.groq.com/openai/v1",
      key_suffix: "cdef",
      last_verified_at: null,
      extraction_enabled: true,
      fallback_active: false,
      custom_endpoints_enabled: false,
      private_endpoints_enabled: false,
      custom_blocked: false,
      classification_byok: true,
      classifier_uses_llm: true,
      classifier_backend: "auto",
      classification_eligible: true,
    });
    const input = {
      provider: "groq" as const,
      api_key: "gsk_live_abcdef",
      model: "llama-3.1-8b-instant",
      classification_byok: true,
    };
    const res = await api.putLlmSettings(input);
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual(input);
    expect(res.classification_byok).toBe(true);
    expect(res.classification_eligible).toBe(true);
  });

  it("in mock mode, threads classification_byok through and reports eligibility", async () => {
    const api = await importMockApi();
    const res = await api.putLlmSettings({
      provider: "mistral",
      api_key: "mistral-key-7890",
      model: "mistral-small",
      classification_byok: true,
    });
    expect(res.classification_byok).toBe(true);
    expect(res.classification_eligible).toBe(true);
  });

  it("passes classification_fallback_local through to the request body when given", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      configured: true,
      provider: "groq",
      model: "llama-3.1-8b-instant",
      base_url: "https://api.groq.com/openai/v1",
      key_suffix: "cdef",
      last_verified_at: null,
      extraction_enabled: true,
      fallback_active: false,
      custom_endpoints_enabled: false,
      private_endpoints_enabled: false,
      custom_blocked: false,
      classification_byok: true,
      classification_fallback_local: true,
      classifier_uses_llm: true,
      classifier_backend: "auto",
      classification_eligible: true,
    });
    const input = {
      provider: "groq" as const,
      api_key: "gsk_live_abcdef",
      model: "llama-3.1-8b-instant",
      classification_byok: true,
      classification_fallback_local: true,
    };
    const res = await api.putLlmSettings(input);
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual(input);
    expect(res.classification_fallback_local).toBe(true);
  });

  it("omits api_key from the request body when left out -- never sends an empty string", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      configured: true,
      provider: "openai",
      model: "gpt-4o-mini",
      base_url: "https://api.openai.com/v1",
      key_suffix: "abcd",
      last_verified_at: null,
      extraction_enabled: true,
      fallback_active: false,
      custom_endpoints_enabled: false,
      private_endpoints_enabled: false,
      custom_blocked: false,
    });
    await api.putLlmSettings({
      provider: "openai",
      model: "gpt-4o-mini",
      classification_byok: true,
    });
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect("api_key" in body).toBe(false);
  });

  it("in mock mode, omitting api_key preserves the existing key_suffix", async () => {
    const api = await importMockApi();
    const first = await api.putLlmSettings({
      provider: "groq",
      api_key: "gsk-live-first-1234",
      model: "llama-3.1-8b-instant",
    });
    expect(first.key_suffix).toBe("1234");

    const second = await api.putLlmSettings({
      provider: "groq",
      model: "llama-3.1-8b-instant",
      classification_byok: true,
    });
    expect(second.key_suffix).toBe("1234");
    expect(second.classification_byok).toBe(true);
  });
});

describe("testLlmSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts to /settings/llm/test", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ ok: true, latency_ms: 210, error: null });
    const res = await api.testLlmSettings();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/settings/llm/test");
    expect((opts as RequestInit).method).toBe("POST");
    expect(res).toEqual({ ok: true, latency_ms: 210, error: null });
  });

  it("in mock mode, always reports ok with a positive latency", async () => {
    const api = await importMockApi();
    const res = await api.testLlmSettings();
    expect(res.ok).toBe(true);
    expect(res.error).toBeNull();
    expect(res.latency_ms).toBeGreaterThan(0);
  });
});

describe("deleteLlmSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("DELETEs /settings/llm and resolves void on a 204", async () => {
    const api = await importLiveApi();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 204, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const res = await api.deleteLlmSettings();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/settings/llm");
    expect((opts as RequestInit).method).toBe("DELETE");
    expect(res).toBeUndefined();
  });

  it("in mock mode, resets the demo settings to unconfigured", async () => {
    const api = await importMockApi();
    await api.deleteLlmSettings();
    const res = await api.getLlmSettings();
    expect(res.configured).toBe(false);
    expect(res.provider).toBeNull();
  });
});

describe("getLlmUsage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("requests GET /settings/llm/usage with the days param, defaulting to 30", async () => {
    const api = await importLiveApi();
    const body = {
      window_days: 30,
      totals: { calls: 12, calls_with_total_tokens: 12, prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
      by_stage: [],
      by_provider: [],
      daily: [],
    };
    const fetchMock = stubFetch(body);
    const res = await api.getLlmUsage();
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/settings/llm/usage");
    expect(url.searchParams.get("days")).toBe("30");
    expect(res).toEqual(body);
  });

  it("passes a non-default days value through", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      window_days: 7,
      totals: { calls: 0, calls_with_total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      by_stage: [],
      by_provider: [],
      daily: [],
    });
    await api.getLlmUsage(7);
    expect(requestedUrl(fetchMock).searchParams.get("days")).toBe("7");
  });

  it("in mock mode, returns non-zero totals so the panel has something to demo", async () => {
    const api = await importMockApi();
    const res = await api.getLlmUsage();
    expect(res.window_days).toBe(30);
    expect(res.totals.calls).toBeGreaterThan(0);
    // Sparse on purpose -- idle days are absent, matching the real API, which
    // only stores days that have usage.
    expect(res.daily.length).toBeGreaterThan(0);
    expect(res.daily.length).toBeLessThan(30);
    expect(res.by_stage.map((s) => s.stage).sort()).toEqual(["classification", "extraction"]);
  });

  it("in mock mode, respects a non-default days window", async () => {
    const api = await importMockApi();
    const res = await api.getLlmUsage(7);
    expect(res.window_days).toBe(7);
    expect(res.daily.length).toBeGreaterThan(0);
    expect(res.daily.length).toBeLessThanOrEqual(7);
  });
});

describe("errorFromResponse (via sendReply)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("keeps legacy string-detail responses working -- message only, no code", async () => {
    const api = await importLiveApi();
    stubFetch({ detail: "Thread not found" }, 404);
    await expect(
      api.sendReply("t1", { bodyText: "hi" }),
    ).rejects.toMatchObject({ status: 404, message: "Thread not found", code: undefined });
  });

  it("reads the structured {code, message} envelope the reply routes use", async () => {
    const api = await importLiveApi();
    stubFetch(
      {
        detail: {
          code: "reply_thread_busy",
          message: "The mailbox is busy syncing -- try again in a moment.",
        },
      },
      409,
    );
    await expect(api.sendReply("t1", { bodyText: "hi" })).rejects.toMatchObject({
      status: 409,
      code: "reply_thread_busy",
      message: "The mailbox is busy syncing -- try again in a moment.",
    });
  });

  it("exposes extra detail fields (e.g. reply_in_flight's attempt_id) for callers that need them", async () => {
    const api = await importLiveApi();
    stubFetch(
      {
        detail: {
          code: "reply_in_flight",
          message: "A reply to this thread is already in progress.",
          attempt_id: "attempt-1",
        },
      },
      409,
    );
    try {
      await api.sendReply("t1", { bodyText: "hi" });
      throw new Error("expected sendReply to reject");
    } catch (e) {
      expect((e as ApiError).code).toBe("reply_in_flight");
      expect((e as ApiError).detail).toEqual({
        code: "reply_in_flight",
        message: "A reply to this thread is already in progress.",
        attempt_id: "attempt-1",
      });
    }
  });

  it("falls back to the status line on a malformed (non-JSON) body, without a code", async () => {
    const api = await importLiveApi();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.sendReply("t1", { bodyText: "hi" })).rejects.toMatchObject({
      status: 502,
      message: "502 Bad Gateway",
      code: undefined,
    });
  });

  it("still reads error_id alongside a legacy string detail", async () => {
    const api = await importLiveApi();
    stubFetch({ detail: "internal error", error_id: "err-abc" }, 500);
    await expect(api.sendReply("t1", { bodyText: "hi" })).rejects.toMatchObject({
      status: 500,
      message: "internal error",
      errorId: "err-abc",
    });
  });
});

describe("sendReply", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts body_text/reply_all/expected_replied_at/override_attempt_id, defaulting the optional fields", async () => {
    const api = await importLiveApi();
    const body = {
      thread_id: "t1",
      message: {
        id: "m1",
        sent_at: "2026-08-10T12:00:00Z",
        sender: "operator@gmail.com",
        recipient: ["alice@stripe.com"],
        cc: null,
        snippet: "hi",
        body_text: "hi",
        body_html: null,
      },
      replied_at: "2026-08-10T12:00:00Z",
      resolved_action_items: 0,
    };
    const fetchMock = stubFetch(body);
    const res = await api.sendReply("t1", { bodyText: "hi" });
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/thread/t1/reply");
    expect((opts as RequestInit).method).toBe("POST");
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({
      body_text: "hi",
      reply_all: false,
      expected_replied_at: null,
      override_attempt_id: null,
    });
    expect(res).toEqual(body);
  });

  it("passes reply_all, expected_replied_at, and override_attempt_id through when given", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({
      thread_id: "t1",
      message: {
        id: "m1",
        sent_at: null,
        sender: null,
        recipient: null,
        cc: null,
        snippet: null,
        body_text: null,
        body_html: null,
      },
      replied_at: "2026-08-10T12:00:00Z",
      resolved_action_items: 2,
    });
    await api.sendReply("t1", {
      bodyText: "hi all",
      replyAll: true,
      expectedRepliedAt: "2026-08-09T00:00:00Z",
      overrideAttemptId: "attempt-9",
    });
    const opts = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(opts.body as string)).toEqual({
      body_text: "hi all",
      reply_all: true,
      expected_replied_at: "2026-08-09T00:00:00Z",
      override_attempt_id: "attempt-9",
    });
  });

  it("in mock mode, marks the thread replied and resolves matching agenda actions", async () => {
    const api = await importMockApi();
    const before = await api.getActions("open", 500);
    const replyAction = before.items.find((a) => a.kind === "reply");
    expect(replyAction).toBeDefined();

    const res = await api.sendReply(replyAction!.thread_id, { bodyText: "on it, thanks" });
    expect(res.replied_at).not.toBeNull();
    expect(res.resolved_action_items).toBeGreaterThan(0);
    expect(res.message.body_text).toBe("on it, thanks");

    const thread = await api.getThread(replyAction!.thread_id);
    expect(thread.thread.replied_at).toBe(res.replied_at);

    const after = await api.getActions("open", 500);
    expect(after.items.find((a) => a.id === replyAction!.id)).toBeUndefined();
  });
});

describe("draftReply", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts an empty body to /mail/thread/{id}/reply-draft", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ draft_text: "Hi,", provider: "openai", model: "gpt-4o-mini" });
    const res = await api.draftReply("t1");
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/thread/t1/reply-draft");
    expect((opts as RequestInit).method).toBe("POST");
    expect((opts as RequestInit).body).toBeUndefined();
    expect(res).toEqual({ draft_text: "Hi,", provider: "openai", model: "gpt-4o-mini" });
  });

  it("in mock mode, drafts against the demo LLM settings when configured", async () => {
    const api = await importMockApi();
    const { items } = await api.getTriage("all", 1);
    const res = await api.draftReply(items[0].thread_id);
    expect(res.draft_text.length).toBeGreaterThan(0);
  });

  it("in mock mode, 409s with the credential-missing code once no key is configured", async () => {
    const api = await importMockApi();
    await api.deleteLlmSettings();
    const { items } = await api.getTriage("all", 1);
    await expect(api.draftReply(items[0].thread_id)).rejects.toMatchObject({
      status: 409,
      code: "reply_draft_credential_missing",
    });
  });
});

describe("getClassifierMix", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("requests GET /settings/llm/classifier-mix and returns the body verbatim", async () => {
    const api = await importLiveApi();
    const body = {
      classifier_mix: [
        { kind: "local", count: 809 },
        { kind: "user_key", count: 86 },
      ],
    };
    const fetchMock = stubFetch(body);
    const res = await api.getClassifierMix();
    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/settings/llm/classifier-mix");
    expect(res).toEqual(body);
  });

  it("in mock mode, returns a non-empty mix", async () => {
    const api = await importMockApi();
    const res = await api.getClassifierMix();
    expect(res.classifier_mix.length).toBeGreaterThan(0);
  });
});
