import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import type { TriageItem } from "@/lib/types";
import { relTime } from "@/lib/time";

interface Props {
  items: TriageItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading?: boolean;
  error?: string | null;
}

export function ThreadList({ items, selectedId, onSelect, loading, error }: Props) {
  if (error) {
    return (
      <div role="alert" className="p-6 text-sm text-destructive font-mono">
        {error}
      </div>
    );
  }
  if (loading && items.length === 0) {
    return (
      <div className="p-6 text-sm text-muted-foreground font-mono">
        loading…
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="p-6 text-sm text-muted-foreground font-mono">
        empty bucket
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border">
      {items.map((it) => {
        const isSel = it.thread_id === selectedId;
        const meta = it.classification.label
          ? LABEL_META[it.classification.label]
          : null;
        const conf = it.classification.confidence;
        const confPct = conf == null ? null : Math.round(conf * 100);
        return (
          <li key={it.thread_id}>
            <button
              data-thread-row={it.thread_id}
              onClick={() => onSelect(it.thread_id)}
              className={[
                "w-full text-left px-3 py-1.5 flex items-center gap-2.5 text-[12.5px] border-l-2",
                isSel
                  ? `${meta ? meta.border : "border-primary"} bg-[var(--color-panel-hi)]`
                  : "border-transparent hover:bg-[var(--color-panel-hi)]/50",
              ].join(" ")}
            >
              {/* label chip */}
              <span
                className={[
                  "shrink-0 px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide font-mono w-[78px] text-center truncate",
                  meta ? meta.chip : "bg-muted text-muted-foreground",
                ].join(" ")}
                title={it.classification.label ?? "unclassified"}
              >
                {meta ? meta.name : "unclass"}
              </span>

              {/* confidence */}
              <div className="shrink-0 flex items-center gap-1.5 w-[78px]">
                <div className="h-1 w-12 rounded-full bg-muted overflow-hidden">
                  <div
                    className={`h-full ${confidenceColor(conf)}`}
                    style={{ width: `${confPct ?? 0}%` }}
                  />
                </div>
                <span
                  className={`text-[10.5px] font-mono tabular-nums w-7 text-right ${confidenceText(conf)}`}
                >
                  {confPct == null ? "—" : `${confPct}%`}
                </span>
              </div>

              {/* subject + snippet + sender */}
              <div className="min-w-0 flex-1 flex items-baseline gap-2 overflow-hidden">
                <span
                  className={[
                    "truncate font-semibold",
                    isSel ? "text-foreground" : "text-foreground/90",
                  ].join(" ")}
                >
                  {it.subject ?? "(no subject)"}
                </span>
                <span className="truncate text-muted-foreground text-[12px]">
                  {it.latest_message_snippet ?? ""}
                </span>
              </div>

              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground font-mono w-10 text-right">
                {relTime(it.last_message_at)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
