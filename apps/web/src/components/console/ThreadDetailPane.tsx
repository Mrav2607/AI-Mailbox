import { useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  MailOpen,
  PanelBottomClose,
  PanelLeftClose,
  PanelRightClose,
  Trash2,
  Undo2,
} from "lucide-react";
import { emailDocument, sanitizeEmailHtml } from "@/lib/email-html";
import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import { emailLocalPart } from "@/lib/sender";
import { absTime } from "@/lib/time";
import type { Classification, Label, ThreadDetail, ThreadMessage } from "@/lib/types";
import { ALL_LABELS } from "@/lib/types";
import type { ReadingSide } from "@/lib/layout";
import { gmailThreadUrl } from "@/lib/utils";
import { PaneDragHandle } from "./ConsoleLayout";

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

function MessageBody({ m }: { m: ThreadMessage }) {
  const [showRemote, setShowRemote] = useState(false);
  const sanitized = useMemo(
    () => (m.body_html ? sanitizeEmailHtml(m.body_html, showRemote) : null),
    [m.body_html, showRemote],
  );
  if (sanitized?.html) {
    return (
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
          auto-sizing the frame to its content (nothing can measure it), hence
          the fixed height and internal scroll. allow-popups + escape-sandbox
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
          className="w-full h-[min(70vh,32rem)] rounded border border-border bg-white"
        />
      </>
    );
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
  // Only worth showing once there's more than one connected account to
  // disambiguate — a single-account mailbox doesn't need it.
  showAccountBadge?: boolean;
  side?: ReadingSide;
  predictionOpen?: boolean;
  onTogglePrediction?: () => void;
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
  showAccountBadge,
  side = "right",
  predictionOpen = true,
  onTogglePrediction,
}: Props) {
  const CollapseIcon = COLLAPSE_ICONS[side];
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

  return (
    <div data-tour="detail-pane" className="h-full flex flex-col">
      <header className="px-4 py-3 border-b border-border bg-[var(--color-panel)] panel-lift">
        <div className="flex items-center gap-2">
          {onBack && <BackToList onBack={onBack} />}
          <div className="flex-1 min-w-0 text-[11px] text-muted-foreground font-mono lowercase truncate">
            {data.thread.provider} · {absTime(data.thread.last_message_at)}
          </div>
          {showAccountBadge && (
            <span
              className="shrink-0 font-mono text-[10px] text-muted-foreground/60 px-1 py-0.5 rounded border border-border/50 truncate max-w-[110px]"
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
                <div className="h-[3px] w-24 rounded-full bg-muted overflow-hidden">
                  <div
                    className={`h-full ${confidenceColor(conf)}`}
                    style={{ width: `${confPct ?? 0}%` }}
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

      {/* Messages */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {data.messages.map((m) => (
          <article
            key={m.id}
            className="px-4 py-3 border-b border-border last:border-b-0"
          >
            <header className="flex items-baseline justify-between gap-2 mb-1.5">
              <span className="font-mono text-[12px] text-foreground/90 truncate">
                {m.sender ?? "(unknown sender)"}
              </span>
              <span className="text-[11px] text-muted-foreground font-mono tabular-nums shrink-0">
                {absTime(m.sent_at)}
              </span>
            </header>
            <MessageBody m={m} />
          </article>
        ))}
      </div>
    </div>
  );
}
