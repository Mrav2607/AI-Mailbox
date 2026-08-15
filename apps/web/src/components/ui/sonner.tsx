import type { ComponentProps } from "react";
import { Toaster as Sonner } from "sonner";

type ToasterProps = ComponentProps<typeof Sonner>;

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="dark"
      position="bottom-right"
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            "group toast font-mono text-[12.5px] group-[.toaster]:bg-[var(--color-panel-hi)] group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:elevated group-[.toaster]:rounded-md",
          description: "group-[.toast]:text-muted-foreground",
          actionButton: "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton: "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
          // The trailing `!` is doing real work. `toast` above already sets
          // `text-foreground`, and these land on the SAME element at the same
          // specificity, so without it the base colour wins on source order and
          // every typed toast renders plain -- success and error included, which
          // is how they've been rendering all along. Verified in the browser,
          // not assumed.
          success: "group-[.toaster]:text-[var(--success)]!",
          error: "group-[.toaster]:text-destructive!",
          // A degraded run (LLM fell back / failed) has to read differently from
          // a routine message at a glance. Reuses the confidence ramp's amber
          // text token, already contrast-checked against this panel in both
          // themes.
          warning: "group-[.toaster]:text-[var(--conf-amber-text)]!",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
