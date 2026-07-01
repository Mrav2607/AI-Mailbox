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
}

export function ThreadDetailPane({
  data,
  classification,
  loading,
  error,
  onReclassify,
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
      <div className="p-8 text-sm text-muted-foreground font-mono flex flex-col gap-2">
        <div>no thread selected</div>
        <div className="text-[11px]">
          use <span className="kbd">j</span> / <span className="kbd">k</span> to
          move, <span className="kbd">↵</span> to open
        </div>
      </div>
    );
  }

  const conf = classification?.confidence ?? null;
  const confPct = conf == null ? null : Math.round(conf * 100);
  const meta = classification?.label ? LABEL_META[classification.label] : null;

  return (
    <div className="h-full flex flex-col">
      <header className="px-4 py-3 border-b border-border bg-[var(--color-panel)]">
        <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-mono">
          {data.thread.provider} · {absTime(data.thread.last_message_at)}
        </div>
        <h2 className="mt-1 text-base font-semibold truncate">
          {data.thread.subject ?? "(no subject)"}
        </h2>
      </header>

      {/* Prediction */}
      <section className="px-4 py-3 border-b border-border bg-[var(--color-panel)]/40">
        <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-mono mb-2">
          prediction
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <span
            className={[
              "px-2 py-0.5 rounded text-[11px] uppercase tracking-wide font-mono",
              meta ? meta.chip : "bg-muted text-muted-foreground",
            ].join(" ")}
          >
            {classification?.label ?? "unclassified"}
          </span>
          <div className="flex items-center gap-1.5">
            <div className="h-1.5 w-24 rounded-full bg-muted overflow-hidden">
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
                className={[
                  "px-1.5 py-0.5 rounded text-[10.5px] uppercase tracking-wide font-mono border transition-colors",
                  active
                    ? `${lm.chip} border-transparent`
                    : `border-border text-muted-foreground hover:${lm.text} hover:border-foreground/30`,
                ].join(" ")}
              >
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
