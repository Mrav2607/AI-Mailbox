# Frontend design prompts — AI Mailbox "Command Center"

Two copy-pasteable system prompts for AI design tools, derived from the live
FastAPI backend (routers in `apps/api/app/routes`, ORM models in
`apps/api/app/db/models`, classifier in `app/services/nlp/classifier.py`).

Both target the same product: a **high-density, keyboard-driven triage console**
for a locally fine-tuned email classifier. Paste the relevant block verbatim.

---

## Prompt A — v0.dev (Next.js + shadcn/ui + Tailwind)

```
You are designing the frontend for "AI Mailbox", a power-user email triage
console backed by a self-hosted FastAPI service and a locally fine-tuned encoder
model. The user is an ML operator, not a casual mail reader. Build a Next.js (App
Router) UI with TypeScript, Tailwind, and shadcn/ui.

## PRODUCT GOAL
A keyboard-first "Command Center" dashboard for reviewing, correcting, and
re-running the model's predictions. Optimize for triage speed and label QA over
inbox aesthetics. Assume a single signed-in operator on a local machine.

## DATA CONTRACTS (match these property names and types EXACTLY)

The model emits exactly six labels (a closed enum — never invent others):
  "needs_reply" | "action_required" | "fyi" | "promotional" | "security_alert" | "spam"

GET /api/v1/mail/triage?bucket={label|all|unclassified}&limit={1..200}
  -> {
       "bucket": "needs_reply",
       "items": [
         {
           "thread_id": "string (uuid)",
           "subject": "string | null",
           "last_message_at": "string (ISO-8601) | null",
           "latest_message_snippet": "string | null",
           "classification": {
             "label": "<one of the 6 labels> | null",   // null = unclassified
             "confidence": 0.0,                           // float 0..1, or null
             "model_version": "string | null"            // e.g. "heuristic-v1", a gemini model id, or a local checkpoint
           }
         }
       ]
     }

GET /api/v1/mail/thread/{thread_id}
  -> {
       "thread": { "id": "uuid", "subject": "string|null", "provider": "string", "last_message_at": "ISO-8601|null" },
       "messages": [
         { "id": "uuid", "sent_at": "ISO-8601|null", "sender": "string|null", "snippet": "string|null", "body_text": "string|null" }
       ]
     }

GET /api/v1/analytics/overview
  -> { "summary": { "threads": 0, "messages": 0, "classified": 0 } }   // all integers

GET /api/v1/auth/me
  -> { "id": "uuid", "email": "string", "display_name": "string | null" }

## API SURFACE (wire interactive state to these; bearer token in localStorage key "ai_mailbox_token", sent as Authorization: Bearer)
  - POST /api/v1/auth/demo-login   body {"email": string}  -> { "access_token": string, "token_type": "bearer", "user": {id,email,display_name} }
  - GET  /api/v1/auth/me           -> validate stored token / restore session
  - GET  /api/v1/mail/triage       -> primary list, refetch on bucket change
  - GET  /api/v1/mail/thread/{id}  -> detail pane on row focus/open
  - POST /api/v1/mail/ingest?max_results={1..500}      -> pulls new mail (long-running; show progress/toast)
  - POST /api/v1/mail/classify/backfill?limit={1..500}&force={bool}  -> classify unlabeled (or force re-label)
  - POST /api/v1/mail/classify/queue?limit={1..500}&force={bool}     -> enqueue async classify -> { "status": "queued", "task_id": string }
  - GET  /api/v1/analytics/overview -> header stat counters
Use TanStack Query: query keys ['triage', bucket], ['thread', id], ['overview'].
Mutations (ingest/backfill/queue) must invalidate ['triage'] and ['overview'] on success.

## LAYOUT — "Command Center" (high density, keyboard-driven)
Three-pane, full-viewport, no marketing chrome. Dark, monospace-leaning,
terminal-adjacent. Tight vertical rhythm (rows ~32-36px), no oversized cards.

  1. LEFT RAIL (~200px): bucket navigator. One row per bucket — the 6 labels plus
     "all" and "unclassified" — each showing a live count. Number-key shortcuts
     1-8 jump buckets. Active bucket highlighted.
  2. CENTER (flex): virtualized thread list for the active bucket. Each row is one
     line: a label chip (color-coded per label), a confidence meter, subject (bold),
     sender, relative time, and the snippet truncated to one line. j/k move the
     selection; Enter opens detail; the selected row has a left accent border.
  3. RIGHT PANE (~40%): detail of the focused thread — subject, provider, the
     message list (sender, time, body_text), and a prediction panel showing label,
     confidence, and model_version. Inline "reclassify" controls: number keys or a
     command menu to override the label (optimistic update).

TOP BAR: app name + signed-in email (from /auth/me); the three overview counters
(Threads / Messages / Classified) as compact stat pills; global actions
"Ingest" and "Backfill" with a busy spinner while their mutations are pending.

COMMAND PALETTE (Cmd/Ctrl-K): fuzzy actions — jump to bucket, ingest N, backfill,
queue classify, reclassify focused thread, copy thread id.

A "?" key opens a keyboard-shortcut cheatsheet overlay.

## CONFIDENCE VISUALIZATION (this is an ML QA tool — make confidence legible)
Render classification.confidence as a 0-100% horizontal micro-bar AND a number.
Color by band: >=0.8 green, 0.5-0.8 amber, <0.5 red. When confidence is null or
label is null, show a dim "unclassified" state. Surface model_version as a small
mono tag so the operator can tell heuristic vs local-model vs LLM predictions
apart at a glance, and let them sort/filter the list by confidence ascending to
find the model's weakest calls fast.

## DESIGN CONSTRAINTS
- Keyboard is the primary input; mouse is secondary. Every list action has a key.
- Color-code the 6 labels consistently everywhere (define the palette once).
- Show empty/loading/error states for every pane. Treat 401 as "session expired".
- No made-up fields. If the API doesn't return it, don't render it.
- Accessibility: focus rings on the active row, aria-live on toasts, role="alert"
  on errors, the shortcut overlay reachable without a mouse.

Start with the three-pane shell, the bucket rail with counts, and the
keyboard-navigable thread list wired to GET /api/v1/mail/triage.
```

---

## Prompt B — Lovable.dev (React + Vite + Tailwind)

```
Build "AI Mailbox", a keyboard-driven Command Center for triaging email that has
been auto-classified by a locally fine-tuned ML model. Use React + Vite +
TypeScript + Tailwind. This connects to an existing self-hosted FastAPI backend
(do NOT add Supabase or a new backend) — call it over fetch with a bearer token
read from localStorage key "ai_mailbox_token". Base URL comes from an env var
VITE_API_BASE_URL (e.g. http://localhost:8000/api/v1). For preview, you may mock
these exact response shapes, but keep the property names and types identical so
swapping in the real API is a no-op.

## WHO IT'S FOR
A single ML operator running the model locally and QA-ing its label quality.
This is an internal tool — favor information density and keyboard speed over
consumer polish.

## EXACT DATA MODEL
The classifier outputs exactly six labels (closed set, never add to it):
  needs_reply, action_required, fyi, promotional, security_alert, spam

Triage list — GET /mail/triage?bucket=<label|all|unclassified>&limit=<1..200>:
  {
    "bucket": "needs_reply",
    "items": [
      {
        "thread_id": "uuid string",
        "subject": "string | null",
        "last_message_at": "ISO-8601 string | null",
        "latest_message_snippet": "string | null",
        "classification": {
          "label": "needs_reply | action_required | fyi | promotional | security_alert | spam | null",
          "confidence": 0.92,            // number 0..1, or null when unclassified
          "model_version": "heuristic-v1 | <gemini model> | <local checkpoint> | null"
        }
      }
    ]
  }

Thread detail — GET /mail/thread/{thread_id}:
  {
    "thread": { "id": "uuid", "subject": "string|null", "provider": "gmail", "last_message_at": "ISO-8601|null" },
    "messages": [
      { "id": "uuid", "sent_at": "ISO-8601|null", "sender": "string|null", "snippet": "string|null", "body_text": "string|null" }
    ]
  }

Overview counters — GET /analytics/overview:
  { "summary": { "threads": 1240, "messages": 5310, "classified": 4980 } }   // integers

Current user — GET /auth/me:
  { "id": "uuid", "email": "string", "display_name": "string | null" }

## API ENDPOINTS TO WIRE (state + actions)
  GET  /auth/me                                  -> on load, validate token; if 401, show login
  POST /auth/demo-login   {"email": "..."}       -> { access_token, token_type:"bearer", user } ; store token, then route to console
  GET  /mail/triage?bucket=&limit=               -> reload list whenever the active bucket changes
  GET  /mail/thread/{id}                          -> load into the detail pane when a thread is opened
  POST /mail/ingest?max_results=<1..500>    -> "Ingest" button; show busy state, then refresh list + counters
  POST /mail/classify/backfill?limit=<1..500>&force=<bool> -> "Backfill" button; classify unlabeled (force = re-label all)
  POST /mail/classify/queue?limit=<1..500>&force=<bool>    -> async variant -> { status:"queued", task_id }
  GET  /analytics/overview                         -> top-bar stat counters
After any ingest/backfill/queue succeeds, re-fetch /mail/triage and
/analytics/overview so counts and rows stay in sync.

## LAYOUT — dense, keyboard-first, three panes
Full-height app, dark theme, compact monospace-flavored type. No landing page,
no hero — drop straight into the console after login.

  • Left sidebar (narrow): a row per bucket — the 6 labels + "all" + "unclassified"
    — each with a live item count. Pressing 1-8 switches buckets. Highlight the active one.
  • Center column: a tight, scrollable (virtualized) list of threads for the active
    bucket. Each row on ONE line: color-coded label chip, a small confidence bar +
    percentage, bold subject, sender, relative timestamp, one-line snippet. Navigate
    with j/k (down/up), open with Enter; the selected row gets a colored left border.
  • Right pane: the focused thread — subject, provider, each message (sender, time,
    body_text), and a "Prediction" box showing label, confidence %, and model_version.
    Include quick "reclassify" buttons (one per label) that optimistically update the row.

  Top bar: app name, the signed-in email, three compact stat pills (Threads /
  Messages / Classified), and the "Ingest" + "Backfill" action buttons with spinners.

  Cmd/Ctrl-K opens a command palette (jump to bucket, ingest, backfill, queue,
  reclassify focused thread). "?" opens a shortcuts cheatsheet.

## CONFIDENCE & MODEL QA (core of the tool)
This dashboard exists to judge the LOCAL MODEL'S output quality:
  - Render confidence as both a 0-100% mini bar and a number; color it
    >=0.8 green, 0.5-0.8 amber, <0.5 red.
  - Show model_version as a small tag so heuristic / local-model / LLM predictions
    are distinguishable at a glance.
  - Let the operator sort the list by confidence ascending to surface the model's
    least-certain predictions for review first.
  - Null label / null confidence renders as a dim "unclassified" state.

## RULES
- Keyboard is the primary input. Every list and bucket action has a shortcut.
- Use ONE consistent color per label across chips, borders, and the sidebar.
- Every pane needs loading, empty, and error states; a 401 means "session expired".
- Never invent fields the API doesn't return.
- Accessible: visible focus on the active row, role="alert" on errors, the
  shortcut overlay usable without a mouse.

Begin with the post-login three-pane shell: bucket sidebar with counts + the
keyboard-navigable thread list bound to GET /mail/triage.
```
