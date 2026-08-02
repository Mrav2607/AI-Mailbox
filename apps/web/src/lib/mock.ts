import type {
  ActionCounts,
  ActionItem,
  ActionsResponse,
  ActionStatus,
  BackfillOptions,
  BackfillResult,
  BucketKey,
  Connection,
  CountsResponse,
  Label,
  LlmProvider,
  LlmSettings,
  LlmTestResult,
  Overview,
  SearchResponse,
  ThreadDetail,
  TriageItem,
  TriageResponse,
  TriageSort,
  User,
} from "./types";
import { ALL_LABELS } from "./types";
import { ApiError } from "./api";

// Two connected Gmail accounts, so preview mode can demo the unified inbox
// and the accounts menu without a live API. Mutable (deleteConnection needs
// to remove one) — same store pattern as ALL/DONE below.
let CONNECTIONS: Connection[] = [
  {
    id: "mock-acct-1",
    provider: "gmail",
    email_address: "operator@gmail.com",
    created_at: new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString(),
    reauth_required: false,
  },
  {
    id: "mock-acct-2",
    provider: "gmail",
    email_address: "ops-archive@gmail.com",
    created_at: new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString(),
    reauth_required: false,
  },
];


const SENDERS = [
  "alice@stripe.com",
  "ops@github.com",
  "security@google.com",
  "newsletter@vercel.com",
  "no-reply@notion.so",
  "team@linear.app",
  "support@aws.amazon.com",
  "deals@uber.com",
  "carol@acme.io",
  "bob@figma.com",
];

const SUBJECTS = [
  "Re: invoice for Q3 services",
  "Production incident postmortem",
  "New sign-in from Chrome on macOS",
  "Your weekly digest is here",
  "Action required: verify your email",
  "50% off this weekend only",
  "Standup notes — engineering",
  "PR #4821 ready for review",
  "Billing receipt #INV-22910",
  "Security alert: unusual activity",
  "Re: dinner Friday?",
  "Calendar invite: design review",
];

const SNIPPETS = [
  "Could you confirm the deliverables before Thursday so we can finalize…",
  "We saw an elevated error rate from 14:02 UTC, root cause was a stale…",
  "If this wasn't you, secure your account immediately by following…",
  "Top stories this week, new launches and tutorials curated for you…",
  "Please review the attached document and respond at your earliest…",
  "Limited time offer ends Sunday — don't miss out on the spring sale…",
];

function rand<T>(arr: T[], i: number): T {
  return arr[i % arr.length];
}

function makeItems(count: number): TriageItem[] {
  const out: TriageItem[] = [];
  const now = Date.now();
  // Gaps accumulate so the list is genuinely recency-ordered — the account
  // sort's stable-sort trick and the date group headers both rely on that.
  // Varied gap sizes spread ~450 items across a month or so of history.
  let minutesAgo = 0;
  for (let i = 0; i < count; i++) {
    minutesAgo += 3 + ((i * 13) % 200);
    const hasLabel = i % 11 !== 0;
    const label = hasLabel ? ALL_LABELS[i % ALL_LABELS.length] : null;
    const conf = hasLabel
      ? Math.max(0.15, Math.min(0.99, 0.4 + ((i * 37) % 65) / 100))
      : null;
    out.push({
      thread_id: `mock-${i}-${(i * 9301 + 49297) % 233280}`,
      subject: rand(SUBJECTS, i + (i % 3)),
      last_message_at: new Date(now - minutesAgo * 60 * 1000).toISOString(),
      latest_message_snippet: rand(SNIPPETS, i),
      latest_message_sender: rand(SENDERS, i),
      classification: {
        label,
        confidence: conf,
        model_version: hasLabel
          ? i % 3 === 0
            ? "heuristic-v1"
            : i % 3 === 1
              ? "local-distilbert-ft-v3"
              : "gemini-1.5-flash"
          : null,
      },
      account_email: CONNECTIONS[i % CONNECTIONS.length].email_address,
    });
  }
  return out;
}



// Big enough to cover a few pages at the app's PAGE_SIZE (200), so infinite
// scroll and offset paging have something real to page through in preview.
const ALL = makeItems(450);

// Thread ids the operator has marked done — the mock's stand-in for the
// server's done_at column. Done threads leave every open bucket.
const DONE = new Set<string>();

function hoursFromNow(h: number): string {
  return new Date(Date.now() + h * 60 * 60 * 1000).toISOString();
}

function daysFromNow(d: number): string {
  return new Date(Date.now() + d * 24 * 60 * 60 * 1000).toISOString();
}

// Demo data for the Agenda view — spans overdue / today / this week / later /
// no-deadline, one low-confidence item (renders the "unverified" treatment),
// and two items sourced from different messages in the SAME thread (the
// agenda selects rows by action id, not thread id, since a thread can carry
// more than one open obligation).
const ACTIONS: ActionItem[] = [
  {
    id: "mock-action-1",
    thread_id: "mock-action-thread-1",
    message_id: "mock-action-msg-1",
    kind: "payment",
    title: "Pay invoice #4821",
    due_at: daysFromNow(-2),
    due_precision: "date",
    due_raw: "due 2 days ago",
    amount: 480,
    currency: "USD",
    source_confidence: 0.82,
    status: "open",
    created_at: daysFromNow(-5),
    thread_subject: "Re: invoice for Q3 services",
    sender: "alice@stripe.com",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "action_required",
  },
  {
    id: "mock-action-2",
    thread_id: "mock-action-thread-2",
    message_id: "mock-action-msg-2",
    kind: "signature",
    title: "Sign the updated contractor agreement",
    due_at: hoursFromNow(4),
    due_precision: "datetime",
    due_raw: "by end of day",
    amount: null,
    currency: null,
    source_confidence: 0.91,
    status: "open",
    created_at: daysFromNow(-1),
    thread_subject: "Contractor agreement — please sign",
    sender: "carol@acme.io",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "action_required",
  },
  {
    id: "mock-action-3",
    thread_id: "mock-action-thread-3",
    message_id: "mock-action-msg-3",
    kind: "rsvp",
    title: "RSVP to the design review sync",
    due_at: daysFromNow(3),
    due_precision: "date",
    due_raw: "by Thursday",
    amount: null,
    currency: null,
    // Below the 0.6 "unverified" threshold on purpose.
    source_confidence: 0.55,
    status: "open",
    created_at: daysFromNow(-1),
    thread_subject: "Calendar invite: design review",
    sender: "bob@figma.com",
    provider: "gmail",
    account_email: CONNECTIONS[1].email_address,
    label: "needs_reply",
  },
  {
    id: "mock-action-4",
    thread_id: "mock-action-thread-4",
    message_id: "mock-action-msg-4",
    kind: "form",
    title: "Complete the vendor security questionnaire",
    due_at: daysFromNow(20),
    due_precision: "date",
    due_raw: "within 3 weeks",
    amount: null,
    currency: null,
    source_confidence: 0.88,
    status: "open",
    created_at: daysFromNow(-2),
    thread_subject: "Vendor security review",
    sender: "support@aws.amazon.com",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "action_required",
  },
  {
    id: "mock-action-5",
    thread_id: "mock-action-thread-5",
    message_id: "mock-action-msg-5",
    kind: "other",
    title: "Review Q3 budget notes",
    due_at: null,
    due_precision: null,
    due_raw: null,
    amount: null,
    currency: null,
    source_confidence: 0.7,
    status: "open",
    created_at: daysFromNow(-3),
    thread_subject: "Standup notes — engineering",
    sender: "team@linear.app",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "needs_reply",
  },
  {
    id: "mock-action-6a",
    thread_id: "mock-action-thread-6",
    message_id: "mock-action-msg-6a",
    kind: "reply",
    title: "Reply with the shipping address",
    due_at: daysFromNow(1),
    due_precision: "datetime",
    due_raw: "tomorrow",
    amount: null,
    currency: null,
    source_confidence: 0.93,
    status: "open",
    created_at: daysFromNow(-1),
    thread_subject: "Order #29104 — a couple of questions",
    sender: "deals@uber.com",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "needs_reply",
  },
  {
    id: "mock-action-6b",
    thread_id: "mock-action-thread-6",
    message_id: "mock-action-msg-6b",
    kind: "payment",
    title: "Confirm the payment method on file",
    due_at: daysFromNow(2),
    due_precision: "date",
    due_raw: "before it ships",
    amount: 129,
    currency: "USD",
    source_confidence: 0.77,
    status: "open",
    created_at: daysFromNow(-1),
    thread_subject: "Order #29104 — a couple of questions",
    sender: "deals@uber.com",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "action_required",
  },
  // Already resolved — exercises the status filter/board beyond "open".
  {
    id: "mock-action-7",
    thread_id: "mock-action-thread-7",
    message_id: "mock-action-msg-7",
    kind: "reply",
    title: "Confirm dinner Friday",
    due_at: daysFromNow(-1),
    due_precision: "date",
    due_raw: "Friday",
    amount: null,
    currency: null,
    source_confidence: 0.85,
    status: "done",
    created_at: daysFromNow(-4),
    thread_subject: "Re: dinner Friday?",
    sender: "bob@figma.com",
    provider: "gmail",
    account_email: CONNECTIONS[0].email_address,
    label: "needs_reply",
  },
];

function mockActionCounts(): ActionCounts {
  const now = Date.now();
  let open = 0;
  let overdue = 0;
  for (const item of ACTIONS) {
    if (item.status !== "open") continue;
    open += 1;
    if (item.due_at && new Date(item.due_at).getTime() < now) overdue += 1;
  }
  return { open, overdue };
}

export function mockActions(status: ActionStatus = "open", limit = 200): ActionsResponse {
  return { items: ACTIONS.filter((a) => a.status === status).slice(0, limit), counts: mockActionCounts() };
}

export function mockSetActionStatus(
  id: string,
  status: ActionStatus,
): { action_id: string; status: ActionStatus; status_at: string | null } {
  const item = ACTIONS.find((a) => a.id === id);
  if (!item) throw new ApiError(404, `Action item not found: ${id}`);
  item.status = status;
  // status_at isn't part of ActionOut/ActionItem (only the status-change
  // response carries it), so it's computed here for the response only, not
  // stored on the item.
  const statusAt = status === "open" ? null : new Date().toISOString();
  return { action_id: id, status: item.status, status_at: statusAt };
}

export function mockBackfillActions(): { status: string; task_id: string } {
  return { status: "queued", task_id: "mock-actions-task-" + Date.now() };
}

export function mockUser(): User {
  return { id: "u_local", email: "operator@local.dev", display_name: "Operator" };
}

export function mockListConnections(): Connection[] {
  return CONNECTIONS.map((c) => ({ ...c }));
}

// Mirrors the server: dropping a connection takes its synced mail with it.
// Returns false (so the caller can 404) when the id isn't a live connection.
export function mockDeleteConnection(id: string): boolean {
  const removed = CONNECTIONS.find((c) => c.id === id);
  if (!removed) return false;
  CONNECTIONS = CONNECTIONS.filter((c) => c.id !== id);
  for (let i = ALL.length - 1; i >= 0; i--) {
    if (ALL[i].account_email === removed.email_address) ALL.splice(i, 1);
  }
  // Agenda rows are keyed off the same account_email — leaving them behind
  // would show obligations for a thread that no longer exists.
  for (let i = ACTIONS.length - 1; i >= 0; i--) {
    if (ACTIONS[i].account_email === removed.email_address) ACTIONS.splice(i, 1);
  }
  return true;
}

export function mockOverview(): Overview {
  const classified = ALL.filter((i) => i.classification.label).length;
  return {
    summary: { threads: ALL.length, messages: ALL.length * 3, classified },
  };
}

// Looks up a connection's email by id. Unknown/disconnected ids resolve to
// undefined so callers can self-scope to "empty results", never throw —
// mirrors the server's contract for a stale provider_account_id.
function connectionEmail(accountId: string): string | undefined {
  return CONNECTIONS.find((c) => c.id === accountId)?.email_address;
}

export function mockTriage(
  bucket: BucketKey,
  limit: number,
  offset = 0,
  accountId?: string | null,
  sort: TriageSort = "recency",
): TriageResponse {
  let items: TriageItem[];
  if (bucket === "done") items = ALL.filter((i) => DONE.has(i.thread_id));
  else {
    const open = ALL.filter((i) => !DONE.has(i.thread_id));
    if (bucket === "all") items = open;
    else if (bucket === "unclassified")
      items = open.filter((i) => !i.classification.label);
    else items = open.filter((i) => i.classification.label === bucket);
  }
  if (accountId) {
    const email = connectionEmail(accountId);
    items = email ? items.filter((i) => i.account_email === email) : [];
  }
  if (sort === "account") {
    // ALL is already in recency order, and Array#sort is stable, so grouping
    // by email alone leaves each group internally sorted by recency for free.
    items = [...items].sort((a, b) => a.account_email.localeCompare(b.account_email));
  }
  return { bucket, items: items.slice(offset, offset + limit) };
}

// Bucket counts stay per-account (accountId scopes them, same as triage);
// `actions` never does — the agenda is always cross-account, mirroring the
// server's `GET /mail/counts` shape.
export function mockCounts(accountId?: string | null): CountsResponse {
  const counts: Record<BucketKey, number> = {
    needs_reply: 0,
    action_required: 0,
    fyi: 0,
    promotional: 0,
    security_alert: 0,
    spam: 0,
    all: 0,
    unclassified: 0,
    done: 0,
  };
  const email = accountId ? connectionEmail(accountId) : undefined;
  // An unknown/disconnected id self-scopes to all-zero counts rather than
  // throwing or silently falling back to the whole mailbox.
  if (accountId && !email) return { counts, actions: mockActionCounts() };
  for (const item of ALL) {
    if (email && item.account_email !== email) continue;
    if (DONE.has(item.thread_id)) {
      counts.done += 1;
      continue;
    }
    counts.all += 1;
    const label = item.classification.label;
    if (label) counts[label] += 1;
    else counts.unclassified += 1;
  }
  return { counts, actions: mockActionCounts() };
}

// Each mock "ingest" delivers exactly two fresh threads — deterministic on
// purpose so preview demos and headless tests can assert exact pill counts.
let ingestSeq = 0;

// accountIds, when given, confines the new mail to those accounts (round-
// robin among just them) instead of the whole connected set — the mock's
// stand-in for a targeted server-side ingest.
export function mockIngest(accountIds?: string[]): number {
  const targets =
    accountIds && accountIds.length > 0
      ? CONNECTIONS.filter((c) => accountIds.includes(c.id))
      : CONNECTIONS;
  if (targets.length === 0) return 0;
  const now = Date.now();
  for (let n = 0; n < 2; n++) {
    ingestSeq += 1;
    ALL.unshift({
      thread_id: `mock-new-${ingestSeq}`,
      subject: `New mail #${ingestSeq}`,
      last_message_at: new Date(now - n * 1000).toISOString(),
      latest_message_snippet: rand(SNIPPETS, ingestSeq),
      latest_message_sender: rand(SENDERS, ingestSeq),
      classification: {
        label: "needs_reply",
        confidence: 0.9,
        model_version: "heuristic-v1",
      },
      account_email: targets[(ingestSeq - 1) % targets.length].email_address,
    });
  }
  return 2;
}

export function mockSetDone(threadId: string, done: boolean) {
  if (done) DONE.add(threadId);
  else DONE.delete(threadId);
}

export function mockThread(id: string): ThreadDetail {
  const item = ALL.find((i) => i.thread_id === id);
  if (!item) throw new ApiError(404, `Thread not found: ${id}`);
  const messages = Array.from({ length: 3 }).map((_, k) => ({
    id: `${id}-m${k}`,
    sent_at: new Date(
      Date.now() - (3 - k) * 1000 * 60 * 60 * 6,
    ).toISOString(),
    sender: rand(SENDERS, k + id.length),
    snippet: rand(SNIPPETS, k + id.length),
    body_text:
      rand(SNIPPETS, k + id.length) +
      "\n\nMore context follows — this is the full message body rendered in the right pane for QA against the model prediction. The operator should be able to scan it quickly and decide whether the label is correct.\n\nThanks,\n" +
      rand(SENDERS, k + id.length),
    // Last message renders as HTML so preview mode exercises that path too.
    body_html:
      k === 2
        ? `<p>${rand(SNIPPETS, k + id.length)}</p><p>This message arrived as <strong>HTML</strong>, so it renders formatted: <a href="https://example.com">a link</a>, a list…</p><ul><li>first point</li><li>second point</li></ul><p>Thanks,<br>${rand(SENDERS, k + id.length)}</p>`
        : null,
  }));
  return {
    thread: {
      id,
      subject: item.subject,
      provider: "gmail",
      // Fake but shaped like Gmail's hex thread ids, so the open-in-Gmail
      // link renders in preview (it just won't resolve to real mail).
      provider_thread_id: id.replace(/-/g, ""),
      last_message_at: item.last_message_at,
      done: DONE.has(id),
      account_email: item.account_email,
    },
    messages,
  };
}

// Mutate the mock store when reclassifying so optimistic updates persist
// across re-fetches in preview.
export function mockApplyLabel(threadId: string, label: Label) {
  const t = ALL.find((i) => i.thread_id === threadId);
  if (t) {
    t.classification = {
      label,
      confidence: 1,
      model_version: "user-override",
    };
  }
}

// Substring match on subject + snippet across the whole mock store, mirroring
// the server's cross-bucket search.
export function mockSearch(
  q: string,
  limit: number,
  accountId?: string | null,
): SearchResponse {
  const needle = q.toLowerCase();
  let items = ALL.filter(
    (i) =>
      (i.subject ?? "").toLowerCase().includes(needle) ||
      (i.latest_message_snippet ?? "").toLowerCase().includes(needle),
  );
  if (accountId) {
    const email = connectionEmail(accountId);
    items = email ? items.filter((i) => i.account_email === email) : [];
  }
  return { query: q, items: items.slice(0, limit) };
}

export function mockDeleteThread(id: string) {
  const idx = ALL.findIndex((i) => i.thread_id === id);
  if (idx >= 0) ALL.splice(idx, 1);
}

// Rough keyword guess so backfilled items land in believable buckets.
function guessLabel(item: TriageItem): Label {
  const s = `${item.subject ?? ""} ${item.latest_message_snippet ?? ""}`.toLowerCase();
  if (/(security|sign-in|alert|unusual|verify your)/.test(s)) return "security_alert";
  if (/(invoice|action required|due|receipt|billing)/.test(s)) return "action_required";
  if (/(% off|sale|deal|weekend|digest|newsletter)/.test(s)) return "promotional";
  if (/(\?|can you|could you|^re:|dinner)/.test(s)) return "needs_reply";
  return "fyi";
}

// Mutate the mock store the way the real backfill would: classify matching
// unclassified items (and, with force, re-label a whole bucket) so the sidebar
// counts and list actually move in preview.
export function mockBackfill(opts: BackfillOptions): BackfillResult {
  const { bucket, limit, force, backend } = opts;
  let pool: TriageItem[];
  if (bucket === "unclassified") pool = ALL.filter((i) => !i.classification.label);
  else if (bucket === "all") pool = ALL;
  else pool = ALL.filter((i) => i.classification.label === bucket);

  const scanned = pool.slice(0, limit);
  const version =
    backend === "local"
      ? "local:email-classifier"
      : backend === "gemini"
        ? "gemini-2.5-flash"
        : "heuristic-v1";

  let created = 0;
  for (const item of scanned) {
    const isNew = !item.classification.label;
    if (!isNew && !force) continue;
    item.classification = {
      label: guessLabel(item),
      confidence: 0.6,
      model_version: version,
    };
    created += 1;
  }
  return { status: "ok", created, scanned: scanned.length };
}

// --- BYOK LLM settings -------------------------------------------------------
// Preset base_urls, mirroring PROVIDER_PRESETS in the API's providers.py --
// a preset write always pins its own base_url, ignoring any caller value,
// same rule the real route enforces.
const PRESET_BASE_URLS: Partial<Record<LlmProvider, string>> = {
  openai: "https://api.openai.com/v1",
  gemini: "https://generativelanguage.googleapis.com/v1beta/openai",
  openrouter: "https://openrouter.ai/api/v1",
  groq: "https://api.groq.com/openai/v1",
  mistral: "https://api.mistral.ai/v1",
};

// Demo mode starts CONFIGURED so preview shows a live settings state instead
// of the empty-state nudge. Mutable, same store pattern as CONNECTIONS above.
// classification_byok starts true on this preset provider (classification_
// eligible follows, true too) so preview shows the "local model handles most
// mail, your key only backs it up" notice from the very first load.
let LLM_SETTINGS: LlmSettings = {
  configured: true,
  provider: "openai",
  model: "gpt-4o-mini",
  base_url: PRESET_BASE_URLS.openai!,
  key_suffix: "sk12",
  last_verified_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
  extraction_enabled: true,
  fallback_active: false,
  custom_endpoints_enabled: false,
  private_endpoints_enabled: false,
  custom_blocked: false,
  classification_byok: true,
  classifier_uses_llm: true,
  classifier_backend: "auto",
  classification_eligible: true,
};

export function mockGetLlmSettings(): LlmSettings {
  return { ...LLM_SETTINGS };
}

// Upserts the demo credential. Only the last 4 chars of the submitted key
// ever get stored, same as the server -- and, like a real PUT, this always
// clears last_verified_at, since a changed credential is unverified again.
export function mockPutLlmSettings(input: {
  provider: LlmProvider;
  api_key: string;
  model: string;
  base_url?: string;
  classification_byok?: boolean;
}): LlmSettings {
  const base_url =
    input.provider === "custom"
      ? (input.base_url ?? LLM_SETTINGS.base_url)
      : (PRESET_BASE_URLS[input.provider] ?? LLM_SETTINGS.base_url);
  // Absent means "unchanged", same as the server. Unlike the real API, this
  // deliberately does NOT force-clear the flag on a switch to "custom" --
  // the demo exists to show both notice states, and a stored
  // classification_byok=true against a custom provider is exactly the
  // out-of-band shape the "isn't set up to sort your mail" notice covers.
  const classificationByok = input.classification_byok ?? LLM_SETTINGS.classification_byok;
  LLM_SETTINGS = {
    ...LLM_SETTINGS,
    configured: true,
    provider: input.provider,
    model: input.model,
    base_url,
    key_suffix: input.api_key.slice(-4),
    last_verified_at: null,
    fallback_active: false,
    classification_byok: classificationByok,
    classification_eligible: input.provider === "custom" ? false : classificationByok,
  };
  return { ...LLM_SETTINGS };
}

// Every mock test call succeeds -- there's no real provider to fail against
// in preview mode.
export function mockTestLlmSettings(): LlmTestResult {
  const latency_ms = 180 + Math.floor(Math.random() * 220);
  LLM_SETTINGS = { ...LLM_SETTINGS, last_verified_at: new Date().toISOString() };
  return { ok: true, latency_ms, error: null };
}

// Resets to unconfigured -- extraction then reads as covered by the
// operator's fallback, mirroring a real deployment with
// ACTION_EXTRACTION_SERVER_FALLBACK on.
export function mockDeleteLlmSettings(): void {
  LLM_SETTINGS = {
    configured: false,
    provider: null,
    model: null,
    base_url: null,
    key_suffix: null,
    last_verified_at: null,
    extraction_enabled: LLM_SETTINGS.extraction_enabled,
    fallback_active: true,
    custom_endpoints_enabled: LLM_SETTINGS.custom_endpoints_enabled,
    private_endpoints_enabled: LLM_SETTINGS.private_endpoints_enabled,
    custom_blocked: false,
    // No credential left to opt in -- classification falls back to the
    // deployment default, same as extraction's fallback story above.
    classification_byok: false,
    classifier_uses_llm: LLM_SETTINGS.classifier_uses_llm,
    classifier_backend: LLM_SETTINGS.classifier_backend,
    classification_eligible: false,
  };
}
