import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import type { BucketKey, Label } from "@/lib/types";
import { ALL_LABELS, BUCKETS } from "@/lib/types";
import { LABEL_META, bucketLabel } from "@/lib/labels";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onBucket: (b: BucketKey) => void;
  onIngest: () => void;
  onBackfill: () => void;
  onQueue: () => void;
  onReclassify: (l: Label) => void;
  hasFocusedThread: boolean;
}

export function CommandPalette({
  open,
  onOpenChange,
  onBucket,
  onIngest,
  onBackfill,
  onQueue,
  onReclassify,
  hasFocusedThread,
}: Props) {
  const run = (fn: () => void) => {
    onOpenChange(false);
    setTimeout(fn, 0);
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="p-0 overflow-hidden max-w-xl border-border bg-[var(--color-panel)]"
      >
        <Command className="bg-transparent">
          <CommandInput placeholder="type a command…" />
          <CommandList className="max-h-[420px]">
            <CommandEmpty>no results</CommandEmpty>
            <CommandGroup heading="buckets">
              {BUCKETS.map((b) => (
                <CommandItem
                  key={b}
                  onSelect={() => run(() => onBucket(b))}
                  value={`jump ${b}`}
                >
                  jump to {bucketLabel(b)}
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandGroup heading="actions">
              <CommandItem onSelect={() => run(onIngest)} value="ingest gmail">
                ingest gmail
              </CommandItem>
              <CommandItem
                onSelect={() => run(onBackfill)}
                value="backfill classification"
              >
                backfill classification
              </CommandItem>
              <CommandItem
                onSelect={() => run(onQueue)}
                value="queue classification"
              >
                queue classification (async)
              </CommandItem>
            </CommandGroup>
            {hasFocusedThread && (
              <CommandGroup heading="reclassify focused">
                {ALL_LABELS.map((l) => (
                  <CommandItem
                    key={l}
                    onSelect={() => run(() => onReclassify(l))}
                    value={`reclassify ${l}`}
                  >
                    set label → {LABEL_META[l].name}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
