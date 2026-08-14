import { Fragment } from "react";
import type { ReactNode } from "react";
import { Check, Inbox, Reply as ReplyIcon } from "lucide-react";
import {
  LABEL_META,
  confidenceColor,
  confidenceText,
  CONF_BAR_TRACK,
  CONF_BAR_FILL,
} from "@/lib/labels";
import { emailLocalPart, senderName } from "@/lib/sender";
import type { TriageItem } from "@/lib/types";
import { dateGroup, relTime } from "@/lib/time";
import type { DateGroup } from "@/lib/time";
import { formatWakeTime } from "@/lib/snooze";
import type { Density } from "@/lib/layout";

interface Props {
  items: TriageItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  showLabel: boolean;
  // Only worth showing once there's more than one connected account to
  // disambiguate — a single-account mailbox doesn't need it.
  showAccount?: boolean;
  narrow?: boolean;
  loading?: boolean;
  error?: string | null;
  density?: Density;
  grouped?: boolean;
  // The snoozed bucket's own time column: a relative wake phrase ("wakes
  // tomorrow 08:00") in place of the usual "when it last had activity"
  // column (docs/plans/2026-08-13-snooze-plan.md §3.6) -- the list is
  // wake-ordered, not recency-ordered, so the recency column would be
  // reading the wrong axis entirely.
  wakeColumn?: boolean;
  isUnseen?: (item: TriageItem) => boolean;
  bulkIds?: ReadonlySet<string>;
  onToggleBulk?: (id: string) => void;
  // Double-click toggles the detail pane open/closed — the row is already
  // selected by the first click, so this doesn't need to know which row.
  onRowDoubleClick?: () => void;
}

function GroupHeader({ label }: { label: DateGroup }) {
  return (
    <li
      className="sticky top-0 z-10 bg-background px-3 py-1 text-[10.5px] font-mono uppercase tracking-wide text-muted-foreground"
    >
      {label}
    </li>
  );
}

function AccountBadge({ email }: { email: string }) {
  return (
    <span
      className="shrink-0 font-mono text-[10px] text-muted-foreground px-1 py-0.5 rounded border border-border/50 truncate max-w-[64px]"
      title={email}
    >
      {emailLocalPart(email)}
    </span>
  );
}

// Same icon language as ThreadDetailPane's header indicator — a small reply
// arrow, muted rather than colored, so it reads as metadata and doesn't
// compete with the classification dot for attention.
function RepliedBadge() {
  return (
    <span
      title="Replied"
      role="img"
      aria-label="Replied"
      className="shrink-0 inline-flex items-center text-muted-foreground/70"
    >
      <ReplyIcon className="h-3 w-3" />
    </span>
  );
}

export function ThreadList({
  items,
  selectedId,
  onSelect,
  showLabel,
  showAccount,
  narrow,
  loading,
  error,
  density = "comfortable",
  grouped = false,
  wakeColumn = false,
  isUnseen,
  bulkIds,
  onToggleBulk,
  onRowDoubleClick,
}: Props) {
  const rowPadY = density === "compact" ? "py-[3px]" : "py-[7px]";
  if (error) {
    return (
      <div role="alert" className="p-6 text-sm text-destructive font-mono">
        {error}
      </div>
    );
  }
  if (loading && items.length === 0) {
    return (
      <ul className="divide-y divide-border" aria-hidden="true">
        {Array.from({ length: 12 }).map((_, i) => (
          <li
            key={i}
            className={`px-3 ${rowPadY} flex items-center gap-2.5 border-l-2 border-transparent`}
          >
            <span className="shrink-0 w-[92px] flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/25 animate-pulse" />
              <span className="h-2.5 w-14 rounded bg-muted animate-pulse" />
            </span>
            <span className="shrink-0 w-[70px] h-2.5 rounded bg-muted animate-pulse" />
            <span
              className="flex-1 h-2.5 rounded bg-muted animate-pulse"
              style={{ maxWidth: `${45 + ((i * 7) % 40)}%` }}
            />
          </li>
        ))}
      </ul>
    );
  }
  if (items.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2 text-muted-foreground font-mono">
        <Inbox className="h-6 w-6 opacity-40" />
        <div className="text-[12.5px]">nothing in this bucket</div>
        <div className="text-[11px]">ingest or backfill to populate it</div>
      </div>
    );
  }
  let prevGroup: DateGroup | null = null;
  return (
    <ul className="divide-y divide-border" aria-label="threads">
      {items.map((it) => {
        const isSel = it.thread_id === selectedId;
        const meta = it.classification.label
          ? LABEL_META[it.classification.label]
          : null;
        const conf = it.classification.confidence;
        const confPct = conf == null ? null : Math.round(conf * 100);
        const sender = senderName(it.latest_message_sender);
        const unseen = isUnseen ? isUnseen(it) : false;
        let header: ReactNode = null;
        if (grouped) {
          const group = dateGroup(it.last_message_at);
          if (group !== prevGroup) {
            header = <GroupHeader key={`group-${group}-${it.thread_id}`} label={group} />;
            prevGroup = group;
          }
        }
        if (narrow) {
          return (
            <Fragment key={it.thread_id}>
              {header}
              <li>
                <button
                  data-thread-row={it.thread_id}
                  onClick={() => onSelect(it.thread_id)}
                  onDoubleClick={onRowDoubleClick}
                  aria-current={isSel ? "true" : undefined}
                  className={[
                    "group relative w-full min-h-14 text-left px-3 py-2.5 flex flex-col justify-center gap-1 text-[12.5px] cursor-pointer select-none",
                    "border-l-2 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:bg-[var(--color-panel-hi)]",
                    isSel
                      ? "border-primary bg-[var(--color-panel-hi)]"
                      : "border-transparent hover:bg-[var(--color-panel-hi)]/45",
                  ].join(" ")}
                >
                  <span className="w-full min-w-0 flex items-center gap-2">
                    {showLabel ? (
                      <span
                        className="min-w-0 flex-1 flex items-center gap-1.5 font-mono"
                        title={it.classification.label ?? "unclassified"}
                      >
                        <span
                          className={[
                            "h-1.5 w-1.5 rounded-full shrink-0",
                            meta ? meta.dot : "bg-muted-foreground/40",
                          ].join(" ")}
                        />
                        <span
                          className={[
                            "truncate text-[12px]",
                            meta ? meta.text : "text-muted-foreground",
                          ].join(" ")}
                        >
                          {meta ? meta.name : "unclass"}
                        </span>
                      </span>
                    ) : (
                      <span
                        className={[
                          "min-w-0 flex-1 font-mono text-[12px] truncate",
                          sender ? "text-foreground" : "text-muted-foreground",
                        ].join(" ")}
                        title={it.latest_message_sender ?? undefined}
                      >
                        {sender ?? "—"}
                      </span>
                    )}
                    <span
                      className={`shrink-0 text-[10.5px] font-mono tabular-nums ${confidenceText(conf)}`}
                    >
                      {confPct == null ? "—" : `${confPct}%`}
                    </span>
                    {it.replied_at && <RepliedBadge />}
                    {showAccount && <AccountBadge email={it.account_email} />}
                    <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground font-mono">
                      {wakeColumn ? formatWakeTime(it.snoozed_until) : relTime(it.last_message_at)}
                    </span>
                  </span>
                  <span className="w-full min-w-0 flex items-baseline gap-2 overflow-hidden">
                    <span
                      className={[
                        "truncate",
                        unseen ? "font-semibold text-foreground" : "text-foreground/90 font-medium",
                      ].join(" ")}
                    >
                      {it.subject ?? "(no subject)"}
                    </span>
                    <span className="truncate text-muted-foreground text-[12px]">
                      {it.latest_message_snippet ?? ""}
                    </span>
                  </span>
                </button>
              </li>
            </Fragment>
          );
        }
        const isBulkSelected = bulkIds?.has(it.thread_id) ?? false;
        return (
          <Fragment key={it.thread_id}>
            {header}
            <li
              className={[
                "group relative flex items-center gap-2.5 pl-3 pr-3 text-[12.5px] cursor-pointer select-none",
                rowPadY,
                "border-l-2 transition-colors duration-150",
                isSel
                  ? "border-primary bg-[var(--color-panel-hi)]"
                  : "border-transparent hover:bg-[var(--color-panel-hi)]/45",
                isBulkSelected ? "bg-primary/10" : "",
              ].join(" ")}
            >
              {onToggleBulk && (
                // A real button, not the row's nested checkbox it used to be —
                // <button> inside <button> is invalid HTML and axe flags it
                // (nested-interactive) once per row. tabIndex={-1} keeps it out
                // of tab order on purpose: the documented keyboard path for
                // bulk-select is the `x` hotkey (see Shortcuts.tsx), and a real
                // tab stop here would add 68+ stops on a full bucket.
                <button
                  type="button"
                  aria-pressed={isBulkSelected}
                  aria-label={`Select thread: ${it.subject ?? "(no subject)"}`}
                  tabIndex={-1}
                  onClick={() => onToggleBulk(it.thread_id)}
                  className={[
                    "shrink-0 h-3.5 w-3.5 rounded-sm border flex items-center justify-center cursor-pointer transition-opacity",
                    isBulkSelected
                      ? "opacity-100 bg-primary border-primary"
                      : "opacity-0 group-hover:opacity-100 border-border",
                  ].join(" ")}
                >
                  {isBulkSelected && <Check className="h-2.5 w-2.5 text-primary-foreground" />}
                </button>
              )}

              <button
                data-thread-row={it.thread_id}
                onClick={() => onSelect(it.thread_id)}
                onDoubleClick={onRowDoubleClick}
                aria-current={isSel ? "true" : undefined}
                className="relative min-w-0 flex-1 self-stretch text-left flex items-center gap-2.5 cursor-pointer select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:bg-[var(--color-panel-hi)]"
              >
                {showLabel ? (
                  <span
                    className="shrink-0 w-[92px] flex items-center gap-1.5 font-mono"
                    title={it.classification.label ?? "unclassified"}
                  >
                    <span
                      className={[
                        "h-1.5 w-1.5 rounded-full shrink-0",
                        meta ? meta.dot : "bg-muted-foreground/40",
                      ].join(" ")}
                    />
                    <span
                      className={[
                        "truncate text-[11px]",
                        meta ? meta.text : "text-muted-foreground",
                      ].join(" ")}
                    >
                      {meta ? meta.name : "unclass"}
                    </span>
                  </span>
                ) : (
                  <span
                    className={[
                      "shrink-0 w-[110px] font-mono text-[11px] truncate",
                      sender ? "text-foreground" : "text-muted-foreground",
                    ].join(" ")}
                    title={it.latest_message_sender ?? undefined}
                  >
                    {sender ?? "—"}
                  </span>
                )}

                {/* confidence: hairline track + mono percent. Spans, not divs:
                    a <button>'s content model is phrasing content only. */}
                <span className="shrink-0 flex items-center gap-1.5 w-[70px]">
                  <span className={`${CONF_BAR_TRACK} w-11`}>
                    <span
                      className={`${CONF_BAR_FILL} ${confidenceColor(conf)}`}
                      // Floor keys off the raw confidence, not the rounded
                      // percent: anything under 0.005 rounds to 0 and would
                      // fall back to a zero-width bar, which is the very
                      // thing the floor exists to prevent.
                      style={{ width: conf ? `max(2px, ${confPct}%)` : "0%" }}
                    />
                  </span>
                  <span
                    className={`text-[10.5px] font-mono tabular-nums w-8 text-right ${confidenceText(conf)}`}
                  >
                    {confPct == null ? "—" : `${confPct}%`}
                  </span>
                </span>

                {/* subject + snippet */}
                <span className="min-w-0 flex-1 flex items-baseline gap-2 overflow-hidden">
                  <span
                    className={[
                      "truncate",
                      isSel || unseen
                        ? "text-foreground font-semibold"
                        : "text-foreground/90 font-medium",
                    ].join(" ")}
                  >
                    {it.subject ?? "(no subject)"}
                  </span>
                  <span className="truncate text-muted-foreground text-[12px]">
                    {it.latest_message_snippet ?? ""}
                  </span>
                </span>

                {it.replied_at && <RepliedBadge />}

                {showAccount && <AccountBadge email={it.account_email} />}

                <span
                  className={[
                    "shrink-0 text-[11px] tabular-nums text-muted-foreground font-mono text-right",
                    // The wake phrase ("wakes tomorrow 08:00") runs longer
                    // than a relative time ("3h") -- the fixed w-9 that fits
                    // the latter would clip it.
                    wakeColumn ? "w-auto whitespace-nowrap" : "w-9",
                  ].join(" ")}
                >
                  {wakeColumn ? formatWakeTime(it.snoozed_until) : relTime(it.last_message_at)}
                </span>
              </button>
            </li>
          </Fragment>
        );
      })}
    </ul>
  );
}
