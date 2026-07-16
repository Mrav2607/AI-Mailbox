import type { ReactNode } from "react";
import { Inbox, List, MailOpen } from "lucide-react";

type NarrowPane = "buckets" | "list" | "reading";

interface Props {
  pane: NarrowPane;
  onPaneChange: (pane: NarrowPane) => void;
  buckets: ReactNode;
  list: ReactNode;
  reading: ReactNode;
}

const TABS = [
  { pane: "buckets", label: "buckets", icon: Inbox },
  { pane: "list", label: "list", icon: List },
  { pane: "reading", label: "reading", icon: MailOpen },
] as const;

export function NarrowShell({
  pane,
  onPaneChange,
  buckets,
  list,
  reading,
}: Props) {
  const activePane =
    pane === "buckets" ? buckets : pane === "list" ? list : reading;

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="flex-1 min-h-0 flex flex-col">{activePane}</div>
      <nav
        aria-label="Panes"
        data-testid="narrow-switcher"
        className="h-12 shrink-0 border-t border-border bg-[var(--color-panel)] flex"
      >
        {TABS.map((tab) => {
          const active = tab.pane === pane;
          const Icon = tab.icon;
          return (
            <button
              key={tab.pane}
              onClick={() => onPaneChange(tab.pane)}
              aria-current={active ? "true" : undefined}
              data-testid={`narrow-tab-${tab.pane}`}
              className={[
                "flex-1 h-full flex flex-col items-center justify-center gap-0.5 cursor-pointer transition-colors",
                active
                  ? "border-t-2 border-primary text-foreground"
                  : "border-t-2 border-transparent text-muted-foreground",
              ].join(" ")}
            >
              <Icon className="h-4 w-4" />
              <span className="font-mono text-[10.5px]">{tab.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
