// Shared micro-type class strings for the console's form-y panels (ingest/
// backfill popovers, AI settings, the settings dialog's usage tab). These
// were three separate copy-pasted consts (TopBar, LlmSettings, LlmUsageCard)
// before the settings-card plan moved all three call sites in one pass --
// cheap to dedup while we're already touching them.
export const fieldLabel = "font-mono text-[11px] text-muted-foreground";
export const control =
  "w-full bg-[var(--color-panel)] border border-border rounded px-2 py-1 text-[12px] font-mono text-foreground";
