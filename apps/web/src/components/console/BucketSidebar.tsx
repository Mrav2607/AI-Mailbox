import { LABEL_META, bucketLabel } from "@/lib/labels";
import type { BucketKey, Label } from "@/lib/types";
import { ALL_LABELS, BUCKETS } from "@/lib/types";
import type { SidebarSide } from "@/lib/layout";
import { PaneDragHandle } from "./ConsoleLayout";
import { Inbox, PanelLeftClose, PanelRightClose } from "lucide-react";

interface Props {
  active: BucketKey;
  counts: Record<BucketKey, number>;
  onSelect: (b: BucketKey) => void;
  onCollapse: () => void;
  side: SidebarSide;
}

export function BucketSidebar({
  active,
  counts,
  onSelect,
  onCollapse,
  side,
}: Props) {
  const CollapseIcon = side === "left" ? PanelLeftClose : PanelRightClose;
  return (
    // Width and the dividing border belong to the layout's Panel/Separator now.
    <aside className="flex-1 min-w-0 bg-[var(--color-panel)] panel-lift flex flex-col">
      <div className="px-3 py-2.5 border-b border-border flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
        <Inbox className="h-3.5 w-3.5" />
        <span className="flex-1">buckets</span>
        <PaneDragHandle source="sidebar" />
        <button
          onClick={onCollapse}
          aria-label="Hide buckets"
          title="Hide buckets ( [ )"
          className="text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
        >
          <CollapseIcon className="h-3.5 w-3.5" />
        </button>
      </div>
      <nav className="flex-1 overflow-y-auto scrollbar-thin py-1">
        {BUCKETS.map((b, i) => {
          const isActive = b === active;
          const meta = (ALL_LABELS as readonly string[]).includes(b)
            ? LABEL_META[b as Label]
            : null;
          return (
            <button
              key={b}
              onClick={() => onSelect(b)}
              className={[
                "relative w-full text-left pl-3 pr-3 py-1.5 flex items-center gap-2 font-mono text-[12.5px] transition-colors duration-100 border-l-2 cursor-pointer focus-visible:outline-none focus-visible:bg-[var(--color-panel-hi)]",
                isActive
                  ? "border-primary bg-[var(--color-panel-hi)] text-foreground"
                  : "border-transparent text-muted-foreground hover:bg-[var(--color-panel-hi)]/60 hover:text-foreground",
              ].join(" ")}
            >
              <span className="kbd">{i + 1}</span>
              <span
                className={[
                  "h-2 w-2 rounded-full shrink-0",
                  meta
                    ? meta.dot
                    : b === "all"
                      ? "bg-foreground/40"
                      : "bg-muted-foreground/40",
                ].join(" ")}
              />
              <span className="flex-1 truncate">{bucketLabel(b)}</span>
              <span className="text-[11px] tabular-nums text-muted-foreground">
                {counts[b] ?? 0}
              </span>
            </button>
          );
        })}
      </nav>
      <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground font-mono">
        <span className="kbd">?</span> shortcuts ·{" "}
        <span className="kbd">⌘K</span> palette
      </div>
    </aside>
  );
}
