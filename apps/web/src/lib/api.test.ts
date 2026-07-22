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

describe("ingestGmail", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts to the renamed /mail/ingest route (not the old /mail/ingest/gmail)", async () => {
    const api = await importLiveApi();
    const fetchMock = stubFetch({ runs: [] });
    await api.ingestGmail();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url as string).pathname).toBe("/api/v1/mail/ingest");
    expect((opts as RequestInit).method).toBe("POST");
  });
});
