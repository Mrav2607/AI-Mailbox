import { afterEach, describe, expect, it, vi } from "vitest";

import {
  allRunsDeduplicated,
  buildSearchQuery,
  buildTriageQuery,
  sumIngestResults,
  waitForSyncRuns,
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
    expect(sumIngestResults(finals)).toEqual({ threads: 4, messages: 11 });
  });

  it("falls back to new_threads when threads_upserted is missing", () => {
    const finals = [run({ result: { status: "ok", new_threads: 2 } })];
    expect(sumIngestResults(finals)).toEqual({ threads: 2, messages: 0 });
  });

  it("treats a run with no result as contributing nothing", () => {
    expect(sumIngestResults([run({ result: null })])).toEqual({ threads: 0, messages: 0 });
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
