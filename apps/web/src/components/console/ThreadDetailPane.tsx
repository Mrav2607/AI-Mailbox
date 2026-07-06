import { MailOpen, PanelRightClose, Trash2 } from "lucide-react";
import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import { absTime } from "@/lib/time";
import type { Classification, Label, ThreadDetail } from "@/lib/types";
import { ALL_LABELS } from "@/lib/types";

interface Props {
  data: ThreadDetail | null;
  classification: Classification | null;
  loading?: boolean;
  error?: string | null;
  onReclassify: (label: Label) => void;
  onCollapse?: () => void;
  onDelete?: () => void;
}

export function ThreadDetailPane({
  data,
  classification,
  loading,
  error,
  onReclassify,
  onCollapse,
  onDelete,
}: Props) {
  if (error) {
    return (
      <div role="alert" className="p-6 text-sm text-destructive font-mono">
        {error}
      </div>
    );
  }
  if (!data && loading) {
    return (
      <div className="p-6 text-sm text-muted-foreground font-mono">
        loading thread…
      </div>
    );
  }
  if (!data) {
    return (
      <div className="h-full flex flex-col">
        {onCollapse && (
          <div className="flex justify-end p-2">
            <button
              onClick={onCollapse}
              aria-label="Hide thread detail"
              title="Hide detail ( ] )"
              className="text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
            >
              <PanelRightClose className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-muted-foreground font-mono">
          <MailOpen className="h-7 w-7 opacity-35" />
          <div className="text-[13px]">no thread selected</div>
          <div className="text-[11px] flex items-center gap-1.5 opacity-80">
            <span className="kbd">j</span> / <span className="kbd">k</span> to
            move · <span className="kbd">↵</span> to open
          </div>
        </div>
      </div>
    );
  }

  const conf = classification?.confidence ?? null;
  const confPct = conf == null ? null : Math.round(conf * 100);
  const meta = classification?.label ? LABEL_META[classification.label] : null;

  return (
    <div className="h-full flex flex-col">
      <header className="px-4 py-3 border-b border-border bg-[var(--color-panel)] panel-lift">
        <div className="flex items-center gap-2">
          <div className="flex-1 min-w-0 text-[11px] text-muted-foreground font-mono lowercase truncate">
            {data.thread.provider} · {absTime(data.thread.last_message_at)}
          </div>
          {onDelete && (
            <button
              onClick={onDelete}
              aria-label="Delete thread"
              title="Delete thread ( # )"
              className="text-muted-foreground hover:text-destructive cursor-pointer transition-colors"
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
              <PanelRightClose className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
        <h2 className="mt-1 text-base font-semibold tracking-tight truncate">
          {data.thread.subject ?? "(no subject)"}
        </h2>
      </header>

      {/* Prediction */}
      <section className="px-4 py-3 border-b border-border bg-[var(--color-panel)]/40">
        <div className="text-[11px] text-muted-foreground font-mono mb-2">
          prediction
        </div>
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
                  "inline-flex items-center gap-1.5 px-2 py-1 rounded text-[10.5px] font-mono border transition-colors duration-100 cursor-pointer",
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
            <pre className="whitespace-pre-wrap font-sans text-[13px] leading-relaxed text-foreground/85">
              {m.body_text ?? m.snippet ?? ""}
            </pre>
          </article>
        ))}
      </div>
    </div>
  );
}
