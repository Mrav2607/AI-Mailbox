import { Inbox } from "lucide-react";
import { LABEL_META, confidenceColor, confidenceText } from "@/lib/labels";
import { senderName } from "@/lib/sender";
import type { TriageItem } from "@/lib/types";
import { relTime } from "@/lib/time";

interface Props {
  items: TriageItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  showLabel: boolean;
  narrow?: boolean;
  loading?: boolean;
  error?: string | null;
}

export function ThreadList({
  items,
  selectedId,
  onSelect,
  showLabel,
  narrow,
  loading,
  error,
}: Props) {
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
            className="px-3 py-[7px] flex items-center gap-2.5 border-l-2 border-transparent"
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
        if (narrow) {
          return (
            <li key={it.thread_id}>
              <button
                data-thread-row={it.thread_id}
                onClick={() => onSelect(it.thread_id)}
                aria-current={isSel ? "true" : undefined}
                className={[
                  "group relative w-full min-h-14 text-left px-3 py-2.5 flex flex-col justify-center gap-1 text-[12.5px] cursor-pointer",
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
                  <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground font-mono">
                    {relTime(it.last_message_at)}
                  </span>
                </span>
                <span className="w-full min-w-0 flex items-baseline gap-2 overflow-hidden">
                  <span className="truncate text-foreground/90 font-medium">
                    {it.subject ?? "(no subject)"}
                  </span>
                  <span className="truncate text-muted-foreground text-[12px]">
                    {it.latest_message_snippet ?? ""}
                  </span>
                </span>
              </button>
            </li>
          );
        }
        return (
          <li key={it.thread_id}>
            <button
              data-thread-row={it.thread_id}
              onClick={() => onSelect(it.thread_id)}
              aria-current={isSel ? "true" : undefined}
              className={[
                "group relative w-full text-left pl-3 pr-3 py-[7px] flex items-center gap-2.5 text-[12.5px] cursor-pointer",
                "border-l-2 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:bg-[var(--color-panel-hi)]",
                isSel
                  ? "border-primary bg-[var(--color-panel-hi)]"
                  : "border-transparent hover:bg-[var(--color-panel-hi)]/45",
              ].join(" ")}
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

              {/* confidence: hairline track + mono percent */}
              <div className="shrink-0 flex items-center gap-1.5 w-[70px]">
                <div className="h-[2px] w-11 bg-border overflow-hidden">
                  <div
                    className={`h-full ${confidenceColor(conf)}`}
                    style={{ width: `${confPct ?? 0}%` }}
                  />
                </div>
                <span
                  className={`text-[10.5px] font-mono tabular-nums w-8 text-right ${confidenceText(conf)}`}
                >
                  {confPct == null ? "—" : `${confPct}%`}
                </span>
              </div>

              {/* subject + snippet */}
              <div className="min-w-0 flex-1 flex items-baseline gap-2 overflow-hidden">
                <span
                  className={[
                    "truncate",
                    isSel
                      ? "text-foreground font-semibold"
                      : "text-foreground/90 font-medium",
                  ].join(" ")}
                >
                  {it.subject ?? "(no subject)"}
                </span>
                <span className="truncate text-muted-foreground text-[12px]">
                  {it.latest_message_snippet ?? ""}
                </span>
              </div>

              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground font-mono w-9 text-right">
                {relTime(it.last_message_at)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
