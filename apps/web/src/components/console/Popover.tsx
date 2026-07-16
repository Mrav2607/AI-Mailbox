import { useEffect, useRef, type ReactNode } from "react";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // The clickable anchor (usually a button). Rendered inline; wiring its
  // onClick to toggle `open` is the caller's job.
  trigger: ReactNode;
  align?: "start" | "end";
  // Optional data-tour tag for the panel, so a walkthrough step can spotlight
  // the whole popover instead of cropping to its inner content.
  panelTour?: string;
  children: ReactNode;
}

// Lightweight anchored dropdown: closes on outside click or Escape. No Radix —
// keeps the bundle lean and matches the console's phosphor styling.
export function Popover({
  open,
  onOpenChange,
  trigger,
  align = "end",
  panelTour,
  children,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onOpenChange(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onOpenChange]);

  return (
    <div ref={ref} className="relative">
      {trigger}
      {open && (
        // No ARIA role on purpose: this is a disclosure (trigger sets
        // aria-expanded, panel sits right after it in the DOM), and
        // role="dialog" would promise modal focus behavior we don't have.
        <div
          data-tour={panelTour}
          className={[
            "absolute top-full mt-1.5 z-50 w-64 rounded-md border border-border bg-[var(--color-panel-hi)] elevated p-3 animate-in fade-in-0 zoom-in-95 duration-150",
            align === "end" ? "right-0 origin-top-right" : "left-0 origin-top-left",
          ].join(" ")}
        >
          {children}
        </div>
      )}
    </div>
  );
}
