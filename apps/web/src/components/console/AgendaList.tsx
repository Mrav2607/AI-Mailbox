import { Fragment } from "react";
import type { RefObject } from "react";
import { Check, ListTodo, X } from "lucide-react";
import { emailLocalPart, senderName } from "@/lib/sender";
import type { AgendaGroup, AgendaGroupKey } from "@/lib/agenda";
import type { ActionItem, ActionKind, ActionStatus } from "@/lib/types";

interface Props {
  groups: AgendaGroup[];
  // The keyboard cursor's action id (j/k move it; Enter opens its thread) —
  // distinct from which thread is actually showing in the detail pane.
  focusedId: string | null;
  onSelect: (item: ActionItem) => void;
  onStatusChange: (id: string, status: ActionStatus) => void;
  // Only worth showing once there's more than one connected account to
  // disambiguate — mirrors ThreadList's showAccount.
  showAccount?: boolean;
  loading?: boolean;
  error?: string | null;
  // Double-click toggles the detail pane open/closed — the row is already
  // selected by the first click, so this doesn't need to know which row.
  onRowDoubleClick?: () => void;
  // True when this user has no LLM credential AND no server fallback covering
  // them -- the empty state then offers a way to fix that instead of just
  // reading as "nothing extracted yet".
  noExtractionCoverage?: boolean;
  onSetupExtraction?: () => void;
  // Whether the agenda has another cursor page to fetch — App owns the
  // fetch and the IntersectionObserver, this just renders (or hides) the
  // bottom sentinel node the observer watches.
  hasMore?: boolean;
  loadingMore?: boolean;
  sentinelRef?: RefObject<HTMLDivElement | null>;
}

const KIND_LABELS: Record<ActionKind, string> = {
  reply: "reply",
  payment: "payment",
  signature: "signature",
  form: "form",
  rsvp: "rsvp",
  deadline: "deadline",
  other: "other",
};

// A `due_precision: "date"` deadline is pinned by the server at 23:59:59 UTC
// of the INTENDED calendar date (see lib/agenda.ts's grouping comment) —
// reading that instant back in the viewer's local zone can roll it onto the
// wrong day, so date-only deadlines format off the UTC components; a real
// datetime deadline shows the exact local instant instead, since there the
// time of day is real.
function formatDue(item: ActionItem): string | null {
  if (!item.due_at) return null;
  const d = new Date(item.due_at);
  if (Number.isNaN(d.getTime())) return null;
  return item.due_precision === "date"
    ? d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function AccountChip({ email }: { email: string }) {
  return (
    <span
      className="shrink-0 font-mono text-[10px] text-muted-foreground px-1 py-0.5 rounded border border-border/50 truncate max-w-[64px]"
      title={email}
    >
      {emailLocalPart(email)}
    </span>
  );
}

function AgendaRow({
  item,
  focused,
  groupKey,
  showAccount,
  onSelect,
  onStatusChange,
  onRowDoubleClick,
}: {
  item: ActionItem;
  focused: boolean;
  groupKey: AgendaGroupKey;
  showAccount?: boolean;
  onSelect: (item: ActionItem) => void;
  onStatusChange: (id: string, status: ActionStatus) => void;
  onRowDoubleClick?: () => void;
}) {
  const due = formatDue(item);
  // A confidence below this threshold gets a muted "unverified" tag instead
  // of being hidden — a low-confidence extraction is still worth surfacing,
  // just not worth trusting blindly.
  const unverified = item.source_confidence != null && item.source_confidence < 0.6;
  const sender = senderName(item.sender);
  const rowTitle = item.title ?? "(untitled action)";
  return (
    // The row highlight and the focus rail live on the <li>, not on the row
    // button: the done/dismiss controls are siblings now, so a background on
    // the button alone would stop short of them and leave a dead strip at the
    // right of a focused row.
    <li
      className={[
        "group flex items-center gap-2.5 pr-2 border-l-2 transition-colors duration-150",
        focused
          ? "border-primary bg-[var(--color-panel-hi)]"
          : "border-transparent hover:bg-[var(--color-panel-hi)]/45",
      ].join(" ")}
    >
      <button
        data-action-row={item.id}
        onClick={() => onSelect(item)}
        onDoubleClick={onRowDoubleClick}
        aria-current={focused ? "true" : undefined}
        className="relative flex-1 min-w-0 self-stretch text-left pl-3 py-[7px] flex items-center gap-2.5 text-[12.5px] cursor-pointer select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:bg-[var(--color-panel-hi)]"
      >
        <span
          className="shrink-0 w-[64px] truncate font-mono text-[11px] text-muted-foreground"
          title={item.kind ?? undefined}
        >
          {item.kind ? KIND_LABELS[item.kind] : "—"}
        </span>
        <span
          className={[
            "shrink-0 w-[64px] font-mono text-[11px] tabular-nums text-right",
            groupKey === "overdue" ? "text-destructive" : "text-muted-foreground",
          ].join(" ")}
        >
          {due ?? "no due date"}
          {groupKey === "overdue" && <span className="sr-only"> (overdue)</span>}
        </span>

        {/* A span, not a div — a <button>'s content model is phrasing content
            only, and this sits inside the row button. */}
        <span className="min-w-0 flex-1 flex items-baseline gap-2 overflow-hidden">
          <span className="truncate text-foreground/90 font-medium">{rowTitle}</span>
          <span className="truncate text-muted-foreground text-[12px]">
            {item.thread_subject ?? ""}
            {sender ? ` · ${sender}` : ""}
          </span>
        </span>

        {unverified && (
          <span className="shrink-0 text-[10px] text-muted-foreground border border-border/50 rounded px-1">
            unverified
          </span>
        )}
        {showAccount && <AccountChip email={item.account_email} />}
      </button>

      {/* Real sibling buttons, not nested inside the row <button> — interactive
          content inside a button is invalid and axe flags it as nested-interactive.
          tabIndex={-1} keeps them out of the tab order: the documented keyboard
          path is the e/x hotkeys (see Shortcuts.tsx), not tabbing through every
          row twice. */}
      <span className="shrink-0 flex items-center gap-1">
        <button
          type="button"
          tabIndex={-1}
          aria-label={`Mark "${rowTitle}" done`}
          title="mark done"
          onClick={() => onStatusChange(item.id, "done")}
          className="h-5 w-5 rounded border border-border flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent cursor-pointer transition-colors"
        >
          <Check className="h-3 w-3" />
        </button>
        <button
          type="button"
          tabIndex={-1}
          aria-label={`Dismiss "${rowTitle}"`}
          title="dismiss"
          onClick={() => onStatusChange(item.id, "dismissed")}
          className="h-5 w-5 rounded border border-border flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent cursor-pointer transition-colors"
        >
          <X className="h-3 w-3" />
        </button>
      </span>
    </li>
  );
}

function GroupHeader({ label, count }: { label: string; count: number }) {
  return (
    <li className="sticky top-0 z-10 bg-background px-3 py-1 text-[10.5px] font-mono uppercase tracking-wide text-muted-foreground flex items-center justify-between">
      <span>{label}</span>
      <span className="tabular-nums normal-case">{count}</span>
    </li>
  );
}

export function AgendaList({
  groups,
  focusedId,
  onSelect,
  onStatusChange,
  showAccount,
  loading,
  error,
  onRowDoubleClick,
  noExtractionCoverage,
  onSetupExtraction,
  hasMore,
  loadingMore,
  sentinelRef,
}: Props) {
  if (error) {
    return (
      <div role="alert" className="p-6 text-sm text-destructive font-mono">
        {error}
      </div>
    );
  }
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  if (loading && total === 0) {
    return (
      <ul className="divide-y divide-border" aria-hidden="true">
        {Array.from({ length: 8 }).map((_, i) => (
          <li
            key={i}
            className="px-3 py-[7px] flex items-center gap-2.5 border-l-2 border-transparent"
          >
            <span className="shrink-0 w-[64px] h-2.5 rounded bg-muted animate-pulse" />
            <span className="shrink-0 w-[64px] h-2.5 rounded bg-muted animate-pulse" />
            <span
              className="flex-1 h-2.5 rounded bg-muted animate-pulse"
              style={{ maxWidth: `${45 + ((i * 7) % 40)}%` }}
            />
          </li>
        ))}
      </ul>
    );
  }
  if (total === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2 text-muted-foreground font-mono">
        <ListTodo className="h-6 w-6 opacity-40" />
        <div className="text-[12.5px]">nothing on the agenda</div>
        <div className="text-[11px]">extracted actions with a deadline show up here</div>
        {noExtractionCoverage && onSetupExtraction && (
          <button
            type="button"
            onClick={onSetupExtraction}
            className="mt-1 h-7 px-3 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary text-[12px] font-mono cursor-pointer transition-colors"
          >
            set up AI extraction
          </button>
        )}
      </div>
    );
  }
  return (
    <>
      <ul className="divide-y divide-border" aria-label="agenda">
        {groups.map((group) =>
          group.items.length === 0 ? null : (
            <Fragment key={group.key}>
              <GroupHeader label={group.label} count={group.items.length} />
              {group.items.map((item) => (
                <AgendaRow
                  key={item.id}
                  item={item}
                  focused={item.id === focusedId}
                  groupKey={group.key}
                  showAccount={showAccount}
                  onSelect={onSelect}
                  onStatusChange={onStatusChange}
                  onRowDoubleClick={onRowDoubleClick}
                />
              ))}
            </Fragment>
          ),
        )}
      </ul>
      {/* The bottom-of-list load-more trigger — App's IntersectionObserver
          watches this node, rooted on the agenda's own scroll container. */}
      {hasMore && (
        <div
          ref={sentinelRef}
          className="h-10 flex items-center justify-center font-mono text-[11px] text-muted-foreground"
        >
          {loadingMore ? "loading more…" : ""}
        </div>
      )}
    </>
  );
}
