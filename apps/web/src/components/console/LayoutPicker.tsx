import { cn } from "@/lib/utils";
import {
  ALL_ARRANGEMENTS,
  arrangementLabel,
  FONT_SCALE_CHOICES,
  sameArrangement,
} from "@/lib/layout";
import type { Arrangement, Density } from "@/lib/layout";

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

const sectionLabel = "font-mono text-[11px] text-muted-foreground";
const segmentButton =
  "h-6 px-2 rounded border cursor-pointer transition-colors font-mono text-[11px]";
const segmentActive = "border-primary/60 phosphor";
const segmentInactive = "border-border hover:border-muted-foreground/50";

const DENSITY_CHOICES: { value: Density; label: string }[] = [
  { value: "comfortable", label: "comfortable" },
  { value: "compact", label: "compact" },
];

const FONT_SCALE_LABELS: Record<(typeof FONT_SCALE_CHOICES)[number], string> = {
  0.9: "A−",
  1: "A",
  1.1: "A+",
};

// loadUi persists any finite 0.75–1.5, but the picker only offers the three
// choices — snap to whichever is nearest instead of requiring exact equality.
function closestFontScale(scale: number): (typeof FONT_SCALE_CHOICES)[number] {
  return FONT_SCALE_CHOICES.reduce((best, choice) =>
    Math.abs(choice - scale) < Math.abs(best - scale) ? choice : best,
  );
}

export function LayoutPicker({
  arrangement,
  onArrangement,
  density,
  onDensity,
  fontScale,
  onFontScale,
}: {
  arrangement: Arrangement;
  onArrangement: (a: Arrangement) => void;
  density?: Density;
  onDensity?: (d: Density) => void;
  fontScale?: number;
  onFontScale?: (s: number) => void;
}) {
  const activeDensity = density ?? "comfortable";
  const activeFontScale = closestFontScale(fontScale ?? 1);
  return (
    <div className="space-y-2.5">
      <div className={sectionLabel}>arrangement</div>
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
      {onDensity && (
        <div className="space-y-1.5">
          <div className={sectionLabel}>density</div>
          <div className="flex gap-2">
            {DENSITY_CHOICES.map(({ value, label }) => {
              const active = value === activeDensity;
              return (
                <button
                  key={value}
                  onClick={() => onDensity(value)}
                  aria-pressed={active}
                  className={cn(segmentButton, active ? segmentActive : segmentInactive)}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {onFontScale && (
        <div className="space-y-1.5">
          <div className={sectionLabel}>text size</div>
          <div className="flex gap-2">
            {FONT_SCALE_CHOICES.map((choice) => {
              const active = choice === activeFontScale;
              return (
                <button
                  key={choice}
                  onClick={() => onFontScale(choice)}
                  aria-pressed={active}
                  aria-label={`text size ${Math.round(choice * 100)}%`}
                  className={cn(segmentButton, active ? segmentActive : segmentInactive)}
                >
                  {FONT_SCALE_LABELS[choice]}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <p className="text-[10.5px] text-muted-foreground font-mono leading-snug">
        or drag a pane by its grip to move it.
      </p>
    </div>
  );
}
