import { Dialog, DialogContent } from "@/components/ui/dialog";

const ROWS: [string, string][] = [
  ["1 – 8", "switch bucket"],
  ["j / k", "next / previous thread"],
  ["↵", "open focused thread"],
  ["g g / G", "jump to top / bottom"],
  ["c", "sort by confidence (asc ↔ desc)"],
  ["r", "re-fetch list + overview"],
  ["i", "ingest gmail"],
  ["b", "backfill classification"],
  ["q", "queue classification"],
  ["⌘ K / Ctrl K", "command palette"],
  ["?", "this cheatsheet"],
  ["Esc", "close overlay"],
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
        <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground mb-2">
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
      </DialogContent>
    </Dialog>
  );
}
