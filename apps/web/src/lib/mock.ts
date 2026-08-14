import type {
  ActionCounts,
  ActionItem,
  ActionsResponse,
  ActionStatus,
  BackfillOptions,
  BackfillResult,
  BucketKey,
  ClassifierMix,
  ClassifierMixEntry,
  Connection,
  CountsResponse,
  Label,
  LlmProvider,
  LlmSettings,
  LlmTestResult,
  LlmUsage,
  LlmUsageByProvider,
  LlmUsageByStage,
  LlmUsageDailyPoint,
  MessageOut,
  Overview,
  ReplyDraft,
  ReplySent,
  SearchResponse,
  ThreadDetail,
  TriageItem,
  TriageResponse,
  TriageSort,
  User,
} from "./types";
import { senderName } from "./sender";
import { ALL_LABELS } from "./types";
import { ApiError, type SendReplyInput } from "./api";

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
    // Spread across the whole 0-1 range, not just the confident end: the
    // confidence bar's low tier only paints at 35% or below, and the old 0.40
    // floor meant preview never rendered it at all.
    const conf = hasLabel ? +(0.08 + ((i * 37) % 92) / 100).toFixed(2) : null;
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
      // Every 7th item starts pre-replied, so the "replied" indicator and
      // the composer's expected_replied_at seeding have something to demo
      // without requiring an actual send in preview mode.
      replied_at: i % 7 === 0 ? new Date(now - minutesAgo * 60 * 1000 + 60_000).toISOString() : null,
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

// Agenda rows must point at threads that genuinely exist in ALL. The detail
// pane fetches the thread by id, and mockThread throws a 404 for anything it
// can't find -- so invented ids made the agenda's reading pane impossible to
// open in preview at all, which is exactly the surface most worth demoing.
//
// The API only lists actions whose source message is still needs_reply or
// action_required, so the preview pool matches that visibility rule too.
const AGENDA_THREADS = ALL.filter(
  (t) =>
    t.classification.label === "needs_reply" ||
    t.classification.label === "action_required",
);

// Fills in every field an agenda row shares with its source thread, so the row
// and the reading pane can never disagree about subject, sender, or account.
function actionFor(
  src: TriageItem,
  fields: Omit<
    ActionItem,
    | "thread_id"
    | "message_id"
    | "provider"
    | "thread_subject"
    | "sender"
    | "account_email"
    | "label"
    | "last_message_at"
  >,
): ActionItem {
  return {
    ...fields,
    thread_id: src.thread_id,
    // mockThread builds three messages per thread with these ids; the last one
    // is the newest, which is what an extractor would have read.
    message_id: `${src.thread_id}-m2`,
    provider: "gmail",
    thread_subject: src.subject,
    sender: src.latest_message_sender,
    account_email: src.account_email,
    label: src.classification.label,
    last_message_at: src.last_message_at,
  };
}

// Demo data for the Agenda view — spans overdue / today / this week / later /
// no-deadline, one low-confidence item (renders the "unverified" treatment),
// and two items sourced from different messages in the SAME thread (the
// agenda selects rows by action id, not thread id, since a thread can carry
// more than one open obligation).
const ACTIONS: ActionItem[] = [
  actionFor(AGENDA_THREADS[0], {
    id: "mock-action-1",
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
  }),
  actionFor(AGENDA_THREADS[1], {
    id: "mock-action-2",
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
  }),
  actionFor(AGENDA_THREADS[2], {
    id: "mock-action-3",
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
  }),
  actionFor(AGENDA_THREADS[3], {
    id: "mock-action-4",
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
  }),
  actionFor(AGENDA_THREADS[4], {
    id: "mock-action-5",
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
  }),
  // Two obligations on one thread — same source, different action ids.
  actionFor(AGENDA_THREADS[5], {
    id: "mock-action-6a",
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
  }),
  actionFor(AGENDA_THREADS[5], {
    id: "mock-action-6b",
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
  }),
  // Already resolved — exercises the status filter/board beyond "open".
  actionFor(AGENDA_THREADS[6], {
    id: "mock-action-7",
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
  }),
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
      replied_at: null,
    });
  }
  return 2;
}

export function mockSetDone(threadId: string, done: boolean) {
  if (done) DONE.add(threadId);
  else DONE.delete(threadId);
  // Mirrors the API: marking a thread done resolves every open action on it
  // (see routes/mailbox.py's set_thread_done), and un-done deliberately does
  // NOT reopen them -- items come back individually via the status route.
  // Preview has to model that, or the agenda looks consistent here and
  // desyncs against the real server.
  if (done) {
    for (const a of ACTIONS) {
      if (a.thread_id === threadId && a.status === "open") a.status = "done";
    }
  }
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
      replied_at: item.replied_at,
    },
    messages,
    classification: item.classification,
  };
}

// A rough, deterministic sent-reply id — good enough for the mock's own
// bookkeeping (React keys, resend idempotency isn't a concern here since
// nothing else references it back).
let replySeq = 0;

// Preview's stand-in for POST /mail/thread/{id}/reply. No stale/in-flight/
// rate-limit choreography (plan §3.5/§3.6) -- this only needs to exercise the
// composer's happy path and the credential-gated draft button, same scope as
// every other mock write path in this file.
export function mockSendReply(threadId: string, input: SendReplyInput): ReplySent {
  const item = ALL.find((i) => i.thread_id === threadId);
  if (!item) throw new ApiError(404, `Thread not found: ${threadId}`);

  const repliedAt = new Date().toISOString();
  item.replied_at = repliedAt;
  replySeq += 1;

  const trimmed = input.bodyText.trim();
  const snippet = trimmed.length > 200 ? `${trimmed.slice(0, 200).trimEnd()}…` : trimmed;
  const message: MessageOut = {
    id: `mock-reply-${threadId}-${replySeq}`,
    sent_at: repliedAt,
    sender: item.account_email,
    recipient: item.latest_message_sender ? [item.latest_message_sender] : null,
    cc: input.replyAll ? [] : null,
    snippet,
    body_text: trimmed,
    body_html: null,
  };

  // Mirrors the real completion transaction's action-resolution step
  // (plan §3.5): every open reply/unkinded obligation on this thread
  // resolves, same as mockSetDone's ACTIONS mutation for a done-thread.
  let resolved = 0;
  for (const a of ACTIONS) {
    if (a.thread_id === threadId && a.status === "open" && (a.kind === "reply" || a.kind === null)) {
      a.status = "done";
      resolved += 1;
    }
  }

  return { thread_id: threadId, message, replied_at: repliedAt, resolved_action_items: resolved };
}

// Preview's stand-in for POST /mail/thread/{id}/reply-draft. Follows the same
// BYOK-gated 409 the real route returns (routes/reply.py's
// resolve_reply_draft_credential) so the composer's disabled-button-with-
// tooltip state is demoable without a live API.
export function mockDraftReply(threadId: string): ReplyDraft {
  const item = ALL.find((i) => i.thread_id === threadId);
  if (!item) throw new ApiError(404, `Thread not found: ${threadId}`);
  if (!LLM_SETTINGS.configured) {
    throw new ApiError(
      409,
      "No LLM credential is configured for drafting replies.",
      undefined,
      "reply_draft_credential_missing",
    );
  }
  const sender = senderName(item.latest_message_sender);
  return {
    draft_text: `Hi ${sender},\n\nThanks for the note — I'll follow up shortly.\n\nBest,`,
    provider: LLM_SETTINGS.provider ?? "openai",
    model: LLM_SETTINGS.model ?? "gpt-4o-mini",
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
      : backend === "llm"
        ? // The real API stamps whichever model the resolved credential names,
          // so read the currently-configured one rather than a fixed string --
          // otherwise a backfill run after changing the model in settings would
          // still report the old one.
          (LLM_SETTINGS.model ?? "llm")
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
  return { status: "ok", created, scanned: scanned.length, skipped_user_overrides: 0 };
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
  // Preview has no operator key, so this tracks eligibility exactly -- which
  // also lets the demo show the backfill form's disabled-LLM state when you
  // untick the classification opt-in.
  classification_llm_usable: true,
};

export function mockGetLlmSettings(): LlmSettings {
  return { ...LLM_SETTINGS };
}

// Upserts the demo credential. Only the last 4 chars of the submitted key
// ever get stored, same as the server. An absent key preserves the demo's
// existing key_suffix -- mirroring the server's "nothing to replace it with"
// rule -- and last_verified_at only clears when the key or another material
// field actually changes, not on a flag-only edit.
export function mockPutLlmSettings(input: {
  provider: LlmProvider;
  api_key?: string;
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
  const key_suffix = input.api_key ? input.api_key.slice(-4) : LLM_SETTINGS.key_suffix;
  const materialChanged =
    Boolean(input.api_key) ||
    input.provider !== LLM_SETTINGS.provider ||
    base_url !== LLM_SETTINGS.base_url ||
    input.model !== LLM_SETTINGS.model;
  LLM_SETTINGS = {
    ...LLM_SETTINGS,
    configured: true,
    provider: input.provider,
    model: input.model,
    base_url,
    key_suffix,
    last_verified_at: materialChanged ? null : LLM_SETTINGS.last_verified_at,
    fallback_active: false,
    classification_byok: classificationByok,
    classification_eligible: input.provider === "custom" ? false : classificationByok,
    classification_llm_usable: input.provider === "custom" ? false : classificationByok,
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
    classification_llm_usable: false,
  };
}

// Deterministic multi-day demo usage so the console's usage card has real
// volume, a real trend, and real gaps to show in preview mode -- weighted
// toward classification, since it fires on every ingested message and
// extraction only runs per action-bearing thread. Every 9th active day, this
// "provider" reports usage for all but two of its classification calls, so
// the disclosure line has something real to render against instead of only
// ever hitting the all-or-nothing branches.
//
// Every 11th day is genuinely idle -- no calls at all -- and gets no `daily`
// entry, same as the real API (it only ever returns rows for days that had
// usage). That's what actually exercises fillDailySeries and the chart's
// gap-filling in preview, instead of a dense series that never has a gap to
// fill in the first place.
function buildMockUsage(days: number): LlmUsage {
  const provider = LLM_SETTINGS.provider ?? "openai";
  const daily: LlmUsageDailyPoint[] = [];
  const stageTotals = {
    classification: { calls: 0, calls_with_total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    extraction: { calls: 0, calls_with_total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };

  for (let i = 0; i < days; i++) {
    // i counts back from "today" at i=0 -- daily[] still ends up oldest-first
    // since we push in the same order the API's own day-ascending list would.
    const dayIndex = days - 1 - i;
    const isWeekend = dayIndex % 7 === 5 || dayIndex % 7 === 6;
    const isIdle = dayIndex % 11 === 0;

    const classificationCalls = isIdle ? 0 : isWeekend ? 3 + (dayIndex % 3) : 14 + (dayIndex % 6);
    const missingTokenCalls =
      !isIdle && dayIndex % 9 === 0 ? Math.min(2, classificationCalls) : 0;
    const classificationCallsWithTokens = classificationCalls - missingTokenCalls;
    const classificationPromptTokens = classificationCallsWithTokens * 220;
    const classificationCompletionTokens = classificationCallsWithTokens * 18;

    const extractionCalls = isIdle || isWeekend ? 0 : dayIndex % 4 === 0 ? 2 : 1;
    const extractionPromptTokens = extractionCalls * 640;
    const extractionCompletionTokens = extractionCalls * 95;

    stageTotals.classification.calls += classificationCalls;
    stageTotals.classification.calls_with_total_tokens += classificationCallsWithTokens;
    stageTotals.classification.prompt_tokens += classificationPromptTokens;
    stageTotals.classification.completion_tokens += classificationCompletionTokens;
    stageTotals.classification.total_tokens += classificationPromptTokens + classificationCompletionTokens;

    stageTotals.extraction.calls += extractionCalls;
    stageTotals.extraction.calls_with_total_tokens += extractionCalls;
    stageTotals.extraction.prompt_tokens += extractionPromptTokens;
    stageTotals.extraction.completion_tokens += extractionCompletionTokens;
    stageTotals.extraction.total_tokens += extractionPromptTokens + extractionCompletionTokens;

    const callsToday = classificationCalls + extractionCalls;
    if (callsToday === 0) continue; // an idle day -- the API wouldn't send a row for it either

    const date = new Date(Date.now() - dayIndex * 24 * 60 * 60 * 1000)
      .toISOString()
      .slice(0, 10);
    daily.push({
      date,
      calls: callsToday,
      total_tokens:
        classificationPromptTokens +
        classificationCompletionTokens +
        extractionPromptTokens +
        extractionCompletionTokens,
    });
  }

  const by_stage: LlmUsageByStage[] = [
    { stage: "classification", ...stageTotals.classification },
    { stage: "extraction", ...stageTotals.extraction },
  ];
  const totals = by_stage.reduce(
    (acc, s) => ({
      calls: acc.calls + s.calls,
      calls_with_total_tokens: acc.calls_with_total_tokens + s.calls_with_total_tokens,
      prompt_tokens: acc.prompt_tokens + s.prompt_tokens,
      completion_tokens: acc.completion_tokens + s.completion_tokens,
      total_tokens: acc.total_tokens + s.total_tokens,
    }),
    { calls: 0, calls_with_total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  );
  const by_provider: LlmUsageByProvider[] = [{ provider, ...totals }];

  return { window_days: days, totals, by_stage, by_provider, daily };
}

export function mockGetLlmUsage(days = 30): LlmUsage {
  return buildMockUsage(days);
}

// Which classifier labeled the mail this demo account "currently has" (plan
// §7) -- point-in-time state, not a history. Mirrors the plan's own measured
// production distribution rather than made-up round numbers. Deliberately
// static and NOT derived from ALL's classification.model_version values --
// those (makeItems above) don't follow the real classifier's model_version
// format, so mapping them through the real prefix rules would misfile them.
// A kind with zero rows is simply absent, same as the real endpoint (a
// GROUP BY never emits an empty group).
export function mockGetClassifierMix(): ClassifierMix {
  const classifier_mix: ClassifierMixEntry[] = [
    { kind: "local", count: 809 },
    { kind: "user_key", count: 86 },
    { kind: "heuristic", count: 11 },
    { kind: "manual", count: 1 },
  ];
  return { classifier_mix };
}
