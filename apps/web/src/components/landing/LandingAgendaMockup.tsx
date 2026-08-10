import { Fragment } from "react";
import { Check, ListTodo } from "lucide-react";

import { emailLocalPart } from "@/lib/sender";
import type { ActionKind } from "@/lib/types";

// Invented sample obligations, not real ones -- picked to show off the group
// ordering (Overdue / Today / This week, same order and labels as
// lib/agenda.ts's groupActions), the kind spread, and the multi-account story
// (two different inboxes feeding one agenda).
interface SampleItem {
  title: string;
  kind: ActionKind;
  due: string;
  overdue?: boolean;
  account: string;
  done?: boolean;
}

interface SampleGroup {
  key: string;
  label: string;
  items: SampleItem[];
}

const GROUPS: SampleGroup[] = [
  {
    key: "overdue",
    label: "Overdue",
    items: [
      {
        title: "Pay fall tuition installment",
        kind: "payment",
        due: "Aug 3",
        overdue: true,
        account: "work@acme.io",
      },
    ],
  },
  {
    key: "today",
    label: "Today",
    items: [
      {
        title: "Sign the lease addendum",
        kind: "signature",
        due: "Aug 8",
        account: "personal@gmail.com",
      },
      {
        title: "RSVP: team offsite Thursday",
        kind: "rsvp",
        due: "Aug 8",
        account: "work@acme.io",
      },
    ],
  },
  {
    key: "this_week",
    label: "This week",
    items: [
      {
        title: "Reply to vendor renewal terms",
        kind: "reply",
        due: "Aug 11",
        account: "work@acme.io",
      },
      {
        title: "Submit Q3 expense report",
        kind: "deadline",
        due: "Aug 13",
        account: "personal@gmail.com",
      },
      {
        title: "Confirm dentist appointment",
        kind: "reply",
        due: "Aug 12",
        account: "personal@gmail.com",
        done: true,
      },
    ],
  },
];

/**
 * A frozen snapshot of the agenda board, used on the landing page to show off
 * the multi-account, multi-kind action list at a glance. Purely decorative:
 * `aria-hidden` and non-interactive, same idiom as LandingConsoleMockup (see
 * that file for why the `dark` class + raw-var arbitrary values are needed
 * here instead of the themed `bg-background`/`text-foreground` utilities).
 */
export function LandingAgendaMockup() {
  return (
    <div
      aria-hidden="true"
      className="dark relative isolate w-full max-w-full select-none overflow-hidden rounded-lg border border-border bg-[var(--panel)] text-foreground elevated"
    >
      {/* CRT scanline texture, see LandingConsoleMockup for why this needs
          `isolate` + a negative z-index to land below the real content. */}
      <div className="landing-scanlines pointer-events-none absolute inset-0 -z-10" />
      <div className="flex items-center gap-2 border-b border-border bg-[var(--panel-hi)] px-3 py-2">
        <ListTodo className="h-3.5 w-3.5 shrink-0 text-muted-foreground/80" />
        <span className="font-mono text-[11px] text-muted-foreground">agenda</span>
        <div className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <span className="kbd">0</span>
        </div>
      </div>
      <ul className="divide-y divide-border">
        {GROUPS.map((group) => (
          <Fragment key={group.key}>
            <li className="flex items-center justify-between bg-[var(--panel-hi)] px-3 py-1 font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              <span>{group.label}</span>
              <span className="tabular-nums normal-case">{group.items.length}</span>
            </li>
            {group.items.map((item) => (
              <li
                key={item.title}
                className={`flex items-center gap-2.5 border-l-2 border-transparent px-3 py-2 ${
                  item.done ? "opacity-50" : ""
                }`}
              >
                <span className="w-14 shrink-0 truncate font-mono text-[10.5px] text-muted-foreground">
                  {item.kind}
                </span>
                <span
                  className={`w-11 shrink-0 font-mono text-[10.5px] tabular-nums text-right ${
                    item.overdue ? "text-destructive" : "text-muted-foreground"
                  }`}
                >
                  {item.due}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-foreground/90">
                  {item.title}
                </span>
                <span
                  className="shrink-0 max-w-[64px] truncate rounded border border-border/50 px-1 py-0.5 font-mono text-[10px] text-muted-foreground/60"
                  title={item.account}
                >
                  {emailLocalPart(item.account)}
                </span>
                <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                  {item.done && <Check className="h-3 w-3 text-[var(--success)]" />}
                </span>
              </li>
            ))}
          </Fragment>
        ))}
      </ul>
    </div>
  );
}
