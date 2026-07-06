import { useEffect, useRef, type ReactNode } from "react";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // The clickable anchor (usually a button). Rendered inline; wiring its
  // onClick to toggle `open` is the caller's job.
  trigger: ReactNode;
  align?: "start" | "end";
  children: ReactNode;
}

// Lightweight anchored dropdown: closes on outside click or Escape. No Radix —
// keeps the bundle lean and matches the console's phosphor styling.
export function Popover({ open, onOpenChange, trigger, align = "end", children }: Props) {
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
        <div
          role="dialog"
          className={[
            "absolute top-full mt-1.5 z-50 w-64 rounded-md border border-border bg-[var(--color-panel-hi)] elevated p-3",
            align === "end" ? "right-0" : "left-0",
          ].join(" ")}
        >
          {children}
        </div>
      )}
    </div>
  );
}
