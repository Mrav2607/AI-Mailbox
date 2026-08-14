import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

// Third field flags rows that only work in the bucket view — the agenda's
// key handler early-returns for these, since it has no search box, sort
// control, or bulk selection.
const ROWS: [string, string, boolean?][] = [
  ["1 – 9", "switch bucket"],
  ["0", "open / close the agenda"],
  ["j / k  ·  ↓ / ↑", "next / previous"],
  ["↵", "open / close focused thread"],
  ["g g / G", "jump to top / bottom"],
  ["e", "mark done"],
  ["z", "snooze / unsnooze the open thread"],
  ["x", "select thread · dismiss action"],
  ["⌘ A / Ctrl A", "select all threads", true],
  ["l then 1 – 6", "relabel focused thread", true],
  ["o", "open focused thread in Gmail"],
  ["Shift R", "reply to the open thread"],
  ["Ctrl ↵", "send the reply while composing"],
  ["/", "search threads (↵ = all buckets)", true],
  ["c", "cycle sort: recent · conf ↑ · conf ↓ · account", true],
  ["[ / ]", "toggle sidebar / detail pane"],
  ["#", "delete focused thread", true],
  ["r", "refresh the current view"],
  ["⌘ K / Ctrl K", "command palette"],
  ["Shift ?", "this cheatsheet"],
  ["Esc", "clear search · close overlay/composer"],
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
        {/* This heading also serves as the dialog's accessible name (Radix
            points aria-labelledby at it) -- promoting it to DialogTitle
            instead of adding a second sr-only one keeps a single heading
            doing both jobs. font-normal/leading-normal override
            DialogTitle's defaults so the visible styling doesn't change. */}
        <DialogTitle className="font-mono font-normal text-[11px] leading-normal tracking-wide text-muted-foreground mb-2">
          keyboard shortcuts
        </DialogTitle>
        <ul className="divide-y divide-border">
          {ROWS.map(([k, label, bucketOnly]) => (
            <li
              key={k}
              className="flex items-center justify-between gap-3 py-1.5 text-[12.5px]"
            >
              <span className="text-foreground/85">
                {label}
                {bucketOnly && (
                  <span className="text-muted-foreground"> · buckets only</span>
                )}
              </span>
              <span className="kbd font-mono">{k}</span>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
