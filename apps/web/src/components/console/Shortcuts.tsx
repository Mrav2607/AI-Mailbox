import { Dialog, DialogContent } from "@/components/ui/dialog";

const ROWS: [string, string][] = [
  ["1 – 9", "switch bucket (9 = done)"],
  ["0", "open / close the agenda"],
  ["j / k", "next / previous thread"],
  ["↵", "open focused thread"],
  ["g g / G", "jump to top / bottom"],
  ["e", "mark done (restore, in the done bucket)"],
  ["x", "select thread (e / # / l act on the selection)"],
  ["l then 1 – 6", "relabel focused thread"],
  ["o", "open focused thread in Gmail (Gmail accounts only)"],
  ["/", "search threads (↵ = all buckets)"],
  ["c", "sort by confidence (asc ↔ desc)"],
  ["[ / ]", "toggle sidebar / detail pane"],
  ["#", "delete focused thread"],
  ["r", "re-fetch list + overview (clears the new-mail pill)"],
  ["i", "ingest mail (quick; click for options)"],
  ["b", "backfill (quick; click for model/bucket)"],
  ["q", "queue classification"],
  ["⌘ K / Ctrl K", "command palette"],
  ["Shift ?", "this cheatsheet"],
  ["Esc", "clear search · close overlay"],
];

// Inside the agenda view j/k/↵/e/x mean something different — they act on
// action rows, not threads — so that view gets its own small reference below
// the main list rather than overloading the rows above.
const AGENDA_ROWS: [string, string][] = [
  ["j / k", "next / previous action"],
  ["↵", "open the focused action's thread"],
  ["e", "mark the focused action done"],
  ["x", "dismiss the focused action"],
  ["0", "back to buckets"],
];

export function Shortcuts({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md bg-[var(--color-panel)] border-border">
        <div className="font-mono text-[11px] tracking-wide text-muted-foreground mb-2">
          keyboard shortcuts
        </div>
        <ul className="divide-y divide-border">
          {ROWS.map(([k, label]) => (
            <li
              key={k}
              className="flex items-center justify-between gap-3 py-1.5 text-[12.5px]"
            >
              <span className="text-foreground/85">{label}</span>
              <span className="kbd font-mono">{k}</span>
            </li>
          ))}
        </ul>
        <div className="font-mono text-[11px] tracking-wide text-muted-foreground mt-3 mb-2">
          in the agenda view
        </div>
        <ul className="divide-y divide-border">
          {AGENDA_ROWS.map(([k, label]) => (
            <li
              key={`agenda-${k}`}
              className="flex items-center justify-between gap-3 py-1.5 text-[12.5px]"
            >
              <span className="text-foreground/85">{label}</span>
              <span className="kbd font-mono">{k}</span>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
