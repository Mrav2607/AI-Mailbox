import type {
  BucketKey,
  Label,
  Overview,
  ThreadDetail,
  TriageItem,
  TriageResponse,
  User,
} from "./types";
import { ALL_LABELS } from "./types";


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
  if (bucket === "all") items = ALL;
  else if (bucket === "unclassified")
    items = ALL.filter((i) => !i.classification.label);
  else items = ALL.filter((i) => i.classification.label === bucket);
  return { bucket, items: items.slice(0, limit) };
}

export function mockThread(id: string): ThreadDetail {
  const item =
    ALL.find((i) => i.thread_id === id) ?? ALL[0];
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
  }));
  return {
    thread: {
      id,
      subject: item.subject,
      provider: "gmail",
      last_message_at: item.last_message_at,
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
      model_version: "operator-override",
    };
  }
}
