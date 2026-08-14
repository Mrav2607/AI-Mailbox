import { useMemo, useState } from "react";
import {
  AlarmClock,
  AlarmClockOff,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  MailOpen,
  PanelBottomClose,
  PanelLeftClose,
  PanelRightClose,
  Reply as ReplyIcon,
  Trash2,
  Undo2,
} from "lucide-react";
import { emailDocument, sanitizeEmailHtml } from "@/lib/email-html";
import {
  LABEL_META,
  confidenceColor,
  confidenceText,
  CONF_BAR_TRACK,
  CONF_BAR_FILL,
} from "@/lib/labels";
import { emailLocalPart } from "@/lib/sender";
import { absTime } from "@/lib/time";
import { SNOOZE_PRESETS } from "@/lib/snooze";
import type { Classification, Label, ReplySent, ThreadDetail, ThreadMessage } from "@/lib/types";
import { ALL_LABELS } from "@/lib/types";
import type { ReadingSide } from "@/lib/layout";
import { gmailThreadUrl } from "@/lib/utils";
import { PaneDragHandle } from "./ConsoleLayout";
import { Popover } from "./Popover";
import { ReplyComposer, type ReplyStateRefetch } from "./ReplyComposer";

const fieldLabel = "font-mono text-[11px] text-muted-foreground";
const control =
  "w-full bg-[var(--color-panel)] border border-border rounded px-2 py-1 text-[12px] font-mono text-foreground";

// The clock button's popover content: presets resolve client-side in local
// time (docs/plans/2026-08-13-snooze-plan.md §3.4/§3.6 -- the server only
// ever sees the resolved absolute instant), plus a native datetime-local
// fallback for anything the presets don't cover.
function SnoozeMenu({ onPick }: { onPick: (d: Date) => void }) {
  const [custom, setCustom] = useState("");
  return (
    <div className="space-y-1.5">
      <div className={fieldLabel}>snooze until</div>
      {SNOOZE_PRESETS.map((p) => (
        <button
          key={p.id}
          type="button"
          onClick={() => onPick(p.resolve(new Date()))}
          className="w-full text-left h-7 px-2 rounded border border-border bg-[var(--color-panel)] hover:bg-accent text-[12px] font-mono cursor-pointer transition-colors"
        >
          {p.label}
        </button>
      ))}
      <label className="block space-y-1 pt-1.5 border-t border-border">
        <span className={fieldLabel}>pick date…</span>
        <input
          type="datetime-local"
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          className={control}
        />
      </label>
      <button
        type="button"
        disabled={!custom}
        onClick={() => {
          // datetime-local's value has no timezone offset -- the Date
          // constructor treats that as local time, same as every preset
          // above, so the instant sent to the server always matches what
          // the picker showed.
          const d = new Date(custom);
          if (!Number.isNaN(d.getTime())) onPick(d);
        }}
        className="w-full h-7 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary-tint-foreground text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
      >
        snooze
      </button>
    </div>
  );
}

const COLLAPSE_ICONS = {
  right: PanelRightClose,
  left: PanelLeftClose,
  bottom: PanelBottomClose,
} as const;

function BackToList({ onBack }: { onBack: () => void }) {
  return (
    <button
      onClick={onBack}
      aria-label="Back to list"
      className="min-h-10 min-w-10 inline-flex items-center gap-1 pr-2 text-muted-foreground hover:text-foreground cursor-pointer transition-colors font-mono text-[11px]"
    >
      <ChevronLeft className="h-4 w-4" />
      list
    </button>
  );
}

function MessageBody({ m, fill }: { m: ThreadMessage; fill?: boolean }) {
  const [showRemote, setShowRemote] = useState(false);
  const sanitized = useMemo(
    () => (m.body_html ? sanitizeEmailHtml(m.body_html, showRemote) : null),
    [m.body_html, showRemote],
  );
  if (sanitized?.html) {
    const frame = (
      <>
        {sanitized.blocked && !showRemote && (
          <button
            onClick={() => setShowRemote(true)}
            className="mb-2 rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:border-foreground/30 cursor-pointer transition-colors"
          >
            remote images blocked — load
          </button>
        )}
        {/*
          The email renders in its own document with an opaque origin, so a
          DOMPurify miss can't reach localStorage (where the session token
          lives) or the console's DOM — it just gets a blank sandbox to itself.
          DOMPurify stays as the first layer; this is the one that holds if it
          ever loses.

          Deliberately NOT here: allow-scripts and allow-same-origin. Either one
          hands back the origin this exists to take away. That also rules out
          auto-sizing the frame to its content (nothing can measure it), so the
          height is derived from the viewport/pane rather than the content, and
          internal scroll handles overflow. allow-popups + escape-sandbox
          are needed or every link in the email dies silently — the sanitizer
          gives them all target="_blank".

          Email HTML assumes a light background whatever the console theme is,
          so the frame paints its own.
        */}
        <iframe
          title="Email message"
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          referrerPolicy="no-referrer"
          srcDoc={emailDocument(sanitized.html, showRemote)}
          className={
            fill
              ? "w-full flex-1 min-h-[20rem] rounded border border-border bg-white"
              : "w-full h-[70vh] rounded border border-border bg-white"
          }
        />
      </>
    );
    if (fill) {
      return <div className="flex-1 min-h-0 flex flex-col">{frame}</div>;
    }
    return frame;
  }
  return (
    <pre className="whitespace-pre-wrap font-sans text-[13px] leading-relaxed text-foreground/85">
      {m.body_text ?? m.snippet ?? ""}
    </pre>
  );
}

interface Props {
  data: ThreadDetail | null;
  classification: Classification | null;
  loading?: boolean;
  error?: string | null;
  onReclassify: (label: Label) => void;
  onBack?: () => void;
  onCollapse?: () => void;
  onDone?: () => void;
  onDelete?: () => void;
  // Snooze wiring (docs/plans/2026-08-13-snooze-plan.md §3.6). Popover open
  // state is lifted to App (P2-5): the custom Popover has no dialog role,
  // so the console hotkey handler's suppression check needs its own signal
  // rather than relying on the hook's role="dialog" backstop. Omitting
  // these together just leaves the clock control off, same pattern as
  // onDone/onDelete.
  onSnooze?: (until: Date) => void;
  onUnsnooze?: () => void;
  snoozePopoverOpen?: boolean;
  onSnoozePopoverOpenChange?: (open: boolean) => void;
  // Only worth showing once there's more than one connected account to
  // disambiguate — a single-account mailbox doesn't need it.
  showAccountBadge?: boolean;
  side?: ReadingSide;
  predictionOpen?: boolean;
  onTogglePrediction?: () => void;
  // Reply composer wiring (docs/plans/2026-08-13-reply-plan.md §3.10). All
  // optional together: omitting them just leaves the Reply control off,
  // which is how this pane already handles onDone/onDelete being absent.
  composerOpen?: boolean;
  onComposerOpenChange?: (open: boolean) => void;
  composerFocusToken?: number;
  onReplySent?: (threadId: string, result: ReplySent) => void;
  onReconnect?: () => void;
  onRefetchReplyState?: (threadId: string) => Promise<ReplyStateRefetch>;
}

export function ThreadDetailPane({
  data,
  classification,
  loading,
  error,
  onReclassify,
  onBack,
  onCollapse,
  onDone,
  onDelete,
  onSnooze,
  onUnsnooze,
  snoozePopoverOpen,
  onSnoozePopoverOpenChange,
  showAccountBadge,
  side = "right",
  predictionOpen = true,
  onTogglePrediction,
  composerOpen,
  onComposerOpenChange,
  composerFocusToken,
  onReplySent,
  onReconnect,
  onRefetchReplyState,
}: Props) {
  const CollapseIcon = COLLAPSE_ICONS[side];
  // The composer needs all three callbacks to function (open/close, send
  // completion, and the stale-reply refetch) -- a caller wiring up only
  // onComposerOpenChange would otherwise get a Reply button that toggles a
  // composer that never actually renders. One flag gates both.
  const composerEnabled = !!(onComposerOpenChange && onReplySent && onRefetchReplyState);
  if (error) {
    if (onBack) {
      return (
        <div data-tour="detail-pane" className="h-full flex flex-col">
          <div className="p-2">
            <BackToList onBack={onBack} />
          </div>
          <div role="alert" className="p-6 text-sm text-destructive font-mono">
            {error}
          </div>
        </div>
      );
    }
    return (
      <div
        data-tour="detail-pane"
        role="alert"
        className="p-6 text-sm text-destructive font-mono"
      >
        {error}
      </div>
    );
  }
  if (!data && loading) {
    if (onBack) {
      return (
        <div data-tour="detail-pane" className="h-full flex flex-col">
          <div className="p-2">
            <BackToList onBack={onBack} />
          </div>
          <div className="p-6 text-sm text-muted-foreground font-mono">
            loading thread…
          </div>
        </div>
      );
    }
    return (
      <div
        data-tour="detail-pane"
        className="p-6 text-sm text-muted-foreground font-mono"
      >
        loading thread…
      </div>
    );
  }
  if (!data) {
    return (
      <div data-tour="detail-pane" className="h-full flex flex-col">
        {onBack ? (
          <div className="flex items-center p-2">
            <BackToList onBack={onBack} />
          </div>
        ) : onCollapse ? (
          <div className="flex justify-end items-center gap-2 p-2">
            <PaneDragHandle source="detail" />
            <button
              onClick={onCollapse}
              aria-label="Hide thread detail"
              title="Hide detail ( ] )"
              className="text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              <CollapseIcon className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-muted-foreground font-mono">
          <MailOpen className="h-7 w-7 opacity-35" />
          <div className="text-[13px]">no thread selected</div>
          {onBack ? (
            <div className="text-[11px] opacity-80">
              tap a thread in the list to open it
            </div>
          ) : (
            <div className="text-[11px] flex items-center gap-1.5 opacity-80">
              <span className="kbd">j</span> / <span className="kbd">k</span> to
              move · <span className="kbd">↵</span> to open
            </div>
          )}
        </div>
      </div>
    );
  }

  const conf = classification?.confidence ?? null;
  const confPct = conf == null ? null : Math.round(conf * 100);
  const meta = classification?.label ? LABEL_META[classification.label] : null;
  // A single HTML message is the common case the fill layout targets — with
  // more than one message there's no single body to stretch to the pane.
  const fillBody = data.messages.length === 1 && !!data.messages[0]?.body_html;

  return (
    <div data-tour="detail-pane" className="h-full flex flex-col">
      <header className="px-4 py-3 border-b border-border bg-[var(--color-panel)] panel-lift">
        <div className="flex items-center gap-2">
          {onBack && <BackToList onBack={onBack} />}
          <div className="flex-1 min-w-0 flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono lowercase truncate">
            <span className="truncate">
              {data.thread.provider} · {absTime(data.thread.last_message_at)}
            </span>
            {data.thread.replied_at && (
              <span
                title={`Replied ${absTime(data.thread.replied_at)}`}
                role="img"
                aria-label={`Replied ${absTime(data.thread.replied_at)}`}
                className="inline-flex shrink-0 items-center text-muted-foreground/80"
              >
                <ReplyIcon className="h-3 w-3" />
              </span>
            )}
          </div>
          {showAccountBadge && (
            <span
              className="shrink-0 font-mono text-[10px] text-muted-foreground px-1 py-0.5 rounded border border-border/50 truncate max-w-[110px]"
              title={data.thread.account_email}
            >
              {emailLocalPart(data.thread.account_email)}
            </span>
          )}
          <PaneDragHandle source="detail" />
          {data.thread.provider === "gmail" && data.thread.provider_thread_id && (
            <a
              href={gmailThreadUrl(data.thread.provider_thread_id, data.thread.account_email)}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open in Gmail"
              title="Open in Gmail ( o )"
              className="inline-flex items-center justify-center max-md:h-10 max-md:w-10 text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
          {composerEnabled && (
            <button
              onClick={() => onComposerOpenChange!(!composerOpen)}
              aria-label={composerOpen ? "Hide reply composer" : "Reply"}
              aria-pressed={!!composerOpen}
              title={composerOpen ? "Hide reply ( Shift R )" : "Reply ( Shift R )"}
              className="inline-flex items-center justify-center max-md:h-10 max-md:w-10 text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              <ReplyIcon className="h-3.5 w-3.5" />
            </button>
          )}
          {onSnooze && onUnsnooze && onSnoozePopoverOpenChange && (
            <Popover
              align="end"
              open={!!snoozePopoverOpen}
              onOpenChange={onSnoozePopoverOpenChange}
              trigger={
                <button
                  onClick={() => {
                    // Binary control (P2-3): once snoozed_until is set, the
                    // control unsnoozes directly -- there's nothing to pick,
                    // so it never opens the preset popover.
                    if (data.thread.snoozed_until) onUnsnooze();
                    else onSnoozePopoverOpenChange(!snoozePopoverOpen);
                  }}
                  aria-label={data.thread.snoozed_until ? "Unsnooze thread" : "Snooze thread"}
                  aria-expanded={data.thread.snoozed_until ? undefined : !!snoozePopoverOpen}
                  title={data.thread.snoozed_until ? "Unsnooze ( z )" : "Snooze ( z )"}
                  className="inline-flex items-center justify-center max-md:h-10 max-md:w-10 text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
                >
                  {data.thread.snoozed_until ? (
                    <AlarmClockOff className="h-3.5 w-3.5" />
                  ) : (
                    <AlarmClock className="h-3.5 w-3.5" />
                  )}
                </button>
              }
            >
              <SnoozeMenu
                onPick={(d) => {
                  onSnoozePopoverOpenChange(false);
                  onSnooze(d);
                }}
              />
            </Popover>
          )}
          {onDone && (
            <button
              onClick={onDone}
              aria-label={data.thread.done ? "Restore thread" : "Mark thread done"}
              title={data.thread.done ? "Restore thread ( e )" : "Mark done ( e )"}
              className="inline-flex items-center justify-center max-md:h-10 max-md:w-10 text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              {data.thread.done ? (
                <Undo2 className="h-3.5 w-3.5" />
              ) : (
                <CheckCircle2 className="h-3.5 w-3.5" />
              )}
            </button>
          )}
          {onDelete && (
            <button
              onClick={onDelete}
              aria-label="Delete thread"
              title="Delete thread ( # )"
              className="inline-flex items-center justify-center max-md:h-10 max-md:w-10 text-muted-foreground hover:text-destructive cursor-pointer transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
          {onCollapse && (
            <button
              onClick={onCollapse}
              aria-label="Hide thread detail"
              title="Hide detail ( ] )"
              className="text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              <CollapseIcon className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
        <h2 className="mt-1 text-base font-semibold tracking-tight truncate">
          {data.thread.subject ?? "(no subject)"}
        </h2>
      </header>

      {/* Prediction — heading doubles as the collapse toggle so the bar can
          get out of the way of long threads. */}
      <section
        data-tour="prediction"
        className="border-b border-border bg-[var(--color-panel)]/40"
      >
        <button
          onClick={onTogglePrediction}
          aria-expanded={predictionOpen}
          title={predictionOpen ? "hide prediction" : "show prediction"}
          className="w-full flex items-center gap-1 px-4 py-2 text-[11px] text-muted-foreground hover:text-foreground font-mono cursor-pointer transition-colors"
        >
          {predictionOpen ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          prediction
        </button>
        {predictionOpen && (
          <div className="px-4 pb-3 animate-in fade-in-0 slide-in-from-top-1 duration-150">
            <div className="flex items-center gap-3 flex-wrap">
              <span
                className={[
                  "inline-flex items-center gap-1.5 px-2 py-1 rounded border font-mono text-[11px]",
                  meta ? `${meta.soft} ${meta.text} ${meta.border}` : "bg-muted text-muted-foreground border-border",
                ].join(" ")}
              >
                <span className={["h-1.5 w-1.5 rounded-full", meta ? meta.dot : "bg-muted-foreground/40"].join(" ")} />
                {classification?.label ?? "unclassified"}
              </span>
              <div className="flex items-center gap-1.5">
                <div className={`${CONF_BAR_TRACK} w-24`}>
                  <div
                    className={`${CONF_BAR_FILL} ${confidenceColor(conf)}`}
                    // Raw confidence, not the rounded percent — under 0.005 it
                    // rounds to 0 and would lose the 2px floor entirely.
                    style={{ width: conf ? `max(2px, ${confPct}%)` : "0%" }}
                  />
                </div>
                <span className={`text-xs font-mono tabular-nums ${confidenceText(conf)}`}>
                  {confPct == null ? "—" : `${confPct}%`}
                </span>
              </div>
              <span className="text-[11px] font-mono text-muted-foreground px-1.5 py-0.5 rounded border border-border">
                {classification?.model_version ?? "no model"}
              </span>
            </div>
            <div className="mt-3 flex items-center gap-1.5 flex-wrap">
              <span className="text-[11px] text-muted-foreground font-mono mr-1">
                reclassify →
              </span>
              {ALL_LABELS.map((l) => {
                const lm = LABEL_META[l];
                const active = classification?.label === l;
                return (
                  <button
                    key={l}
                    onClick={() => onReclassify(l)}
                    aria-pressed={active}
                    className={[
                      "inline-flex items-center gap-1.5 px-2 py-1 max-md:min-h-10 rounded text-[10.5px] font-mono border transition-colors duration-150 cursor-pointer",
                      active
                        ? `${lm.soft} ${lm.text} ${lm.border}`
                        : "border-border text-muted-foreground hover:text-foreground hover:border-foreground/30",
                    ].join(" ")}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${lm.dot}`} />
                    {lm.name}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </section>

      {/* Messages. tabIndex + role/aria-label make this reachable and
          nameable for keyboard/AT users -- a long thread scrolls but had
          no focusable child and no name, so a keyboard-only user had no
          way to scroll it at all. */}
      <div
        role="region"
        aria-label="Thread messages"
        tabIndex={0}
        className="flex-1 overflow-y-auto scrollbar-thin flex flex-col"
      >
        {data.messages.map((m) => (
          <article
            key={m.id}
            className={[
              "px-4 py-3 border-b border-border last:border-b-0",
              fillBody ? "flex-1 min-h-0 flex flex-col" : "shrink-0",
            ].join(" ")}
          >
            <header className={["flex items-baseline justify-between gap-2 mb-1.5", fillBody ? "shrink-0" : ""].join(" ")}>
              <span className="font-mono text-[12px] text-foreground/90 truncate">
                {m.sender ?? "(unknown sender)"}
              </span>
              <span className="text-[11px] text-muted-foreground font-mono tabular-nums shrink-0">
                {absTime(m.sent_at)}
              </span>
            </header>
            <MessageBody m={m} fill={fillBody} />
            {m.pending && (
              <p className="mt-1.5 text-[10.5px] font-mono text-muted-foreground italic">
                sending — will appear in the thread after your mailbox syncs
              </p>
            )}
          </article>
        ))}
      </div>

      {composerEnabled && (
        <ReplyComposer
          key={data.thread.id}
          threadId={data.thread.id}
          provider={data.thread.provider}
          repliedAt={data.thread.replied_at}
          messages={data.messages}
          selfAddress={data.thread.account_email}
          open={!!composerOpen}
          onOpenChange={onComposerOpenChange!}
          onSent={onReplySent!}
          onReconnect={onReconnect}
          onRefetchReplyState={onRefetchReplyState!}
          focusToken={composerFocusToken}
        />
      )}
    </div>
  );
}
