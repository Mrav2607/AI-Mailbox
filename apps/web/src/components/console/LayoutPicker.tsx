import { cn } from "@/lib/utils";
import { ALL_ARRANGEMENTS, arrangementLabel, sameArrangement } from "@/lib/layout";
import type { Arrangement } from "@/lib/layout";

// Mini diagram of one arrangement: sidebar sliver, list block, and the
// reading pane in amber (the console's single accent).
function Thumb({ a }: { a: Arrangement }) {
  const sb = <div className="w-[16%] rounded-[1px] bg-foreground/25" />;
  const inner =
    a.reading === "bottom" ? (
      <div className="flex-1 flex flex-col gap-px min-w-0">
        <div className="flex-[1.3] rounded-[1px] bg-foreground/25" />
        <div className="flex-1 rounded-[1px] bg-primary/35" />
      </div>
    ) : (
      <div
        className={cn(
          "flex-1 flex gap-px min-w-0",
          a.reading === "left" && "flex-row-reverse",
        )}
      >
        <div className="flex-[1.4] rounded-[1px] bg-foreground/25" />
        <div className="flex-1 rounded-[1px] bg-primary/35" />
      </div>
    );
  return a.sidebar === "left" ? (
    <>
      {sb}
      {inner}
    </>
  ) : (
    <>
      {inner}
      {sb}
    </>
  );
}

export function LayoutPicker({
  arrangement,
  onArrangement,
}: {
  arrangement: Arrangement;
  onArrangement: (a: Arrangement) => void;
}) {
  return (
    <div className="space-y-2.5">
      <div className="font-mono text-[11px] text-muted-foreground">arrangement</div>
      <div className="grid grid-cols-3 gap-2">
        {ALL_ARRANGEMENTS.map((a) => {
          const active = sameArrangement(a, arrangement);
          return (
            <button
              key={arrangementLabel(a)}
              onClick={() => onArrangement(a)}
              aria-label={arrangementLabel(a)}
              aria-pressed={active}
              title={arrangementLabel(a)}
              className={cn(
                "h-10 flex gap-px p-1 rounded border cursor-pointer transition-colors",
                active
                  ? "border-primary/60 phosphor"
                  : "border-border hover:border-muted-foreground/50",
              )}
            >
              <Thumb a={a} />
            </button>
          );
        })}
      </div>
      <p className="text-[10.5px] text-muted-foreground font-mono leading-snug">
        or drag a pane by its grip to move it.
      </p>
    </div>
  );
}
