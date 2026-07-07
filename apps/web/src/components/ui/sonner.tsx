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
          success: "group-[.toaster]:text-[oklch(0.82_0.13_150)]",
          error: "group-[.toaster]:text-[oklch(0.74_0.17_25)]",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
