import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
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
import type { Arrangement } from "@/lib/layout";
import { THEME_PREFS } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onBucket: (b: BucketKey) => void;
  onIngest: () => void;
  onBackfill: () => void;
  onQueue: () => void;
  onReclassify: (l: Label) => void;
  hasFocusedThread: boolean;
  onToggleSidebar: () => void;
  onToggleDetail: () => void;
  onTogglePrediction: () => void;
  onTheme: (t: ThemePref) => void;
  onArrangement: (patch: Partial<Arrangement>) => void;
  onFocusSearch: () => void;
  onDelete: () => void;
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
  onToggleSidebar,
  onToggleDetail,
  onTogglePrediction,
  onTheme,
  onArrangement,
  onFocusSearch,
  onDelete,
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
        {/* Screen-reader-only name for the dialog; Radix warns without one. */}
        <DialogTitle className="sr-only">command palette</DialogTitle>
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
            <CommandGroup heading="view">
              <CommandItem
                onSelect={() => run(onFocusSearch)}
                value="search threads find"
              >
                search threads
              </CommandItem>
              <CommandItem
                onSelect={() => run(onToggleSidebar)}
                value="toggle buckets sidebar"
              >
                toggle bucket sidebar
              </CommandItem>
              <CommandItem
                onSelect={() => run(onToggleDetail)}
                value="toggle thread detail pane"
              >
                toggle thread detail pane
              </CommandItem>
              <CommandItem
                onSelect={() => run(onTogglePrediction)}
                value="toggle prediction reclassify bar"
              >
                toggle prediction bar
              </CommandItem>
              {THEME_PREFS.map((t) => (
                <CommandItem
                  key={`theme-${t}`}
                  onSelect={() => run(() => onTheme(t))}
                  value={`theme ${t}`}
                >
                  theme: {t}
                </CommandItem>
              ))}
              {(["left", "right"] as const).map((side) => (
                <CommandItem
                  key={`sb-${side}`}
                  onSelect={() => run(() => onArrangement({ sidebar: side }))}
                  value={`layout buckets sidebar ${side}`}
                >
                  layout: buckets → {side}
                </CommandItem>
              ))}
              {(["right", "bottom", "left"] as const).map((side) => (
                <CommandItem
                  key={`rd-${side}`}
                  onSelect={() => run(() => onArrangement({ reading: side }))}
                  value={`layout reading pane ${side}`}
                >
                  layout: reading pane → {side}
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandGroup heading="actions">
              <CommandItem onSelect={() => run(onIngest)} value="ingest gmail">
                ingest gmail…
              </CommandItem>
              <CommandItem
                onSelect={() => run(onBackfill)}
                value="backfill classification"
              >
                backfill classification…
              </CommandItem>
              <CommandItem
                onSelect={() => run(onQueue)}
                value="queue classification"
              >
                queue classification (async)
              </CommandItem>
            </CommandGroup>
            {hasFocusedThread && (
              <CommandGroup heading="focused thread">
                {ALL_LABELS.map((l) => (
                  <CommandItem
                    key={l}
                    onSelect={() => run(() => onReclassify(l))}
                    value={`reclassify ${l}`}
                  >
                    set label → {LABEL_META[l].name}
                  </CommandItem>
                ))}
                <CommandItem
                  onSelect={() => run(onDelete)}
                  value="delete thread"
                  className="text-destructive"
                >
                  delete thread
                </CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
