import type {
  BackfillOptions,
  BackfillResult,
  BucketKey,
  Label,
  Overview,
  ThreadDetail,
  TriageItem,
  TriageResponse,
  User,
} from "./types";
import { ALL_LABELS } from "./types";
import { ApiError } from "./api";


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
  for (let i = 0; i < count; i++) {
    const hasLabel = i % 11 !== 0;
    const label = hasLabel ? ALL_LABELS[i % ALL_LABELS.length] : null;
    const conf = hasLabel
      ? Math.max(0.15, Math.min(0.99, 0.4 + ((i * 37) % 65) / 100))
      : null;
    out.push({
      thread_id: `mock-${i}-${(i * 9301 + 49297) % 233280}`,
      subject: rand(SUBJECTS, i + (i % 3)),
      last_message_at: new Date(now - i * 1000 * 60 * (3 + (i % 47))).toISOString(),
      latest_message_snippet: rand(SNIPPETS, i),
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
    });
  }
  return out;
}



const ALL = makeItems(140);

// Thread ids the operator has marked done — the mock's stand-in for the
// server's done_at column. Done threads leave every open bucket.
const DONE = new Set<string>();

export function mockUser(): User {
  return { id: "u_local", email: "operator@local.dev", display_name: "Operator" };
}

export function mockOverview(): Overview {
  const classified = ALL.filter((i) => i.classification.label).length;
  return {
    summary: { threads: ALL.length, messages: ALL.length * 3, classified },
  };
}

export function mockTriage(bucket: BucketKey, limit: number): TriageResponse {
  let items: TriageItem[];
  if (bucket === "done") items = ALL.filter((i) => DONE.has(i.thread_id));
  else {
    const open = ALL.filter((i) => !DONE.has(i.thread_id));
    if (bucket === "all") items = open;
    else if (bucket === "unclassified")
      items = open.filter((i) => !i.classification.label);
    else items = open.filter((i) => i.classification.label === bucket);
  }
  return { bucket, items: items.slice(0, limit) };
}

export function mockCounts(): Record<BucketKey, number> {
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
  for (const item of ALL) {
    if (DONE.has(item.thread_id)) {
      counts.done += 1;
      continue;
    }
    counts.all += 1;
    const label = item.classification.label;
    if (label) counts[label] += 1;
    else counts.unclassified += 1;
  }
  return counts;
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
): { query: string; items: TriageItem[] } {
  const needle = q.toLowerCase();
  const items = ALL.filter(
    (i) =>
      (i.subject ?? "").toLowerCase().includes(needle) ||
      (i.latest_message_snippet ?? "").toLowerCase().includes(needle),
  ).slice(0, limit);
  return { query: q, items };
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
