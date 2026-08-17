import { useRef, useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { GoogleMark } from "./GoogleMark";
import { MicrosoftMark } from "./MicrosoftMark";
import { LabelSyncRow } from "./LabelSyncRow";
import { LlmSettingsSection, type LlmSettingsSectionProps } from "./LlmSettings";
import { LlmUsageSection, type LlmUsageSectionProps } from "./LlmUsageCard";
import { cn } from "@/lib/utils";
import type { AccountSyncHealth, SyncHealth } from "@/lib/api";
import type { Connection, SettingsTab } from "@/lib/types";

// Canonical definition lives in lib/types so lib code (use-llm-panel) never
// has to import from components; re-exported here for the dialog's consumers.
export type { SettingsTab } from "@/lib/types";

const TABS: { key: SettingsTab; label: string }[] = [
  { key: "accounts", label: "accounts" },
  { key: "ai", label: "ai model" },
  { key: "usage", label: "usage" },
];

// Same shape+color+aria system as TopBar's account status dots
// (TopBar.tsx:396-425) -- deliberate a11y semantics (filled/hollow/diamond),
// duplicated here rather than imported since TopBar is out of this wave's
// file boundary. W2 dedups both call sites onto one shared copy.
type AccountStatus = "ok" | "amber" | "red";

const ACCOUNT_STATUS_DOT: Record<AccountStatus, string> = {
  ok: "rounded-full bg-[var(--success)]",
  amber: "rounded-full border border-[var(--primary)] bg-transparent",
  red: "rotate-45 bg-destructive",
};

const ACCOUNT_STATUS_LABEL: Record<AccountStatus, string> = {
  ok: "syncing fine",
  amber: "stale or syncing",
  red: "reauth required",
};

function accountStatus(connection: Connection, accounts: AccountSyncHealth[]): AccountStatus {
  const match = accounts.find((a) => a.provider_account_id === connection.id);
  if (connection.reauth_required || match?.reason === "reauth_required") return "red";
  if (!match || match.stale || match.sync_in_progress) return "amber";
  return "ok";
}

/**
 * The accounts tab: one bordered card per connection with its status,
 * provider mark, email, connected date, two-step disconnect, and label-sync
 * row -- the management UI the popover used to carry (settings-card plan
 * §3.2). Kept local to this file rather than exported; nothing else needs it
 * yet.
 */
function AccountsSection({
  connections,
  health,
  onConnectGmail,
  onConnectOutlook,
  onDisconnect,
  onConnectionUpdated,
}: {
  connections: Connection[];
  health: SyncHealth | null;
  onConnectGmail: () => void;
  onConnectOutlook?: () => void;
  onDisconnect: (connectionId: string) => void;
  onConnectionUpdated: (connection: Connection) => void;
}) {
  // Two-step confirm: first click arms it, second fires. Resets for free on
  // remount (dialog close), same as the popover's own version did on close.
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const accounts = health?.accounts ?? [];

  return (
    <div className="space-y-2.5">
      {connections.length === 0 ? (
        <p className="text-[11px] text-muted-foreground font-mono">no accounts connected</p>
      ) : (
        <ul
          className="space-y-2"
          // Same escape hatch as the popover: a click anywhere else in the
          // list disarms an armed confirm.
          onClick={() => setConfirmingId(null)}
        >
          {connections.map((c) => {
            const status = accountStatus(c, accounts);
            const confirming = confirmingId === c.id;
            return (
              <li key={c.id} className="rounded border border-border p-3 space-y-2">
                <div className="flex items-center gap-2">
                  {/* role="img" + aria-label puts the status text in the
                      accessibility tree on its own -- a title alone only
                      reaches a sighted mouse user hovering long enough. */}
                  <span
                    role="img"
                    aria-label={ACCOUNT_STATUS_LABEL[status]}
                    title={ACCOUNT_STATUS_LABEL[status]}
                    className={cn("h-1.5 w-1.5 shrink-0", ACCOUNT_STATUS_DOT[status])}
                  />
                  <span className="shrink-0 flex items-center" title={c.provider}>
                    {c.provider === "outlook" ? <MicrosoftMark /> : <GoogleMark />}
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground/90"
                    title={c.email_address}
                  >
                    {c.email_address}
                  </span>
                  <button
                    type="button"
                    onClick={(e) => {
                      // Don't let this reach the list's onClick, which
                      // disarms on any click outside the button itself.
                      e.stopPropagation();
                      if (confirming) {
                        setConfirmingId(null);
                        onDisconnect(c.id);
                      } else {
                        // Arming a different account's confirm disarms this
                        // one -- there's only ever one confirmingId.
                        setConfirmingId(c.id);
                      }
                    }}
                    title={
                      confirming
                        ? "disconnect — deletes its synced mail; reconnecting resyncs from scratch"
                        : "disconnect this account"
                    }
                    className={cn(
                      "shrink-0 font-mono text-[10.5px] px-1.5 py-0.5 rounded border cursor-pointer transition-colors",
                      confirming
                        ? "border-destructive/60 bg-destructive/15 text-destructive"
                        : "border-border text-muted-foreground hover:text-destructive hover:border-destructive/40",
                    )}
                  >
                    {confirming ? "confirm" : "disconnect"}
                  </button>
                </div>
                <div className="pl-3.5 flex flex-wrap items-center gap-x-1.5 text-[10.5px] font-mono text-muted-foreground">
                  <span>{ACCOUNT_STATUS_LABEL[status]}</span>
                  <span aria-hidden="true">·</span>
                  <span>connected {new Date(c.created_at).toLocaleDateString()}</span>
                </div>
                {/* Visible, not just a hover title -- a touch device (or
                    anyone who doesn't hover) still has to see this before a
                    mail-deleting confirm. */}
                {confirming && (
                  <p className="pl-3.5 text-[10.5px] font-mono text-destructive leading-snug">
                    deletes this account&apos;s synced mail — reconnecting resyncs from scratch
                  </p>
                )}
                <LabelSyncRow
                  connection={c}
                  onConnectGmail={onConnectGmail}
                  onConnectOutlook={onConnectOutlook}
                  onUpdated={onConnectionUpdated}
                />
              </li>
            );
          })}
        </ul>
      )}
      <div className="flex flex-col gap-1.5">
        <button
          type="button"
          onClick={onConnectGmail}
          className="w-full h-7 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent text-[12px] font-mono cursor-pointer transition-colors"
        >
          connect another gmail
        </button>
        {onConnectOutlook && (
          <button
            type="button"
            onClick={onConnectOutlook}
            className="w-full h-7 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent text-[12px] font-mono cursor-pointer transition-colors"
          >
            connect outlook
          </button>
        )}
      </div>
    </div>
  );
}

export interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  tab: SettingsTab;
  onTabChange: (tab: SettingsTab) => void;

  // accounts tab
  connections: Connection[];
  health: SyncHealth | null;
  onConnectGmail: () => void;
  onConnectOutlook?: () => void;
  onDisconnect: (connectionId: string) => void;
  // Threaded to every card's LabelSyncRow.onUpdated -- App merges the PATCH
  // response into its own `connections` state so the popover's status line
  // and this dialog's toggle agree without waiting on the next poll
  // (settings-card plan §3.2/P1-1).
  onConnectionUpdated: (connection: Connection) => void;

  // ai tab -- same field names LlmSettingsSection takes minus `open` (this
  // dialog supplies that itself) and `onOpenUsage` (wired to a tab switch
  // here instead of a prop from the caller).
  settings: LlmSettingsSectionProps["settings"];
  settingsError: NonNullable<LlmSettingsSectionProps["settingsError"]>;
  onRetrySettings: NonNullable<LlmSettingsSectionProps["onRetrySettings"]>;
  onSave: LlmSettingsSectionProps["onSave"];
  saving: LlmSettingsSectionProps["saving"];
  onTest: LlmSettingsSectionProps["onTest"];
  testing: LlmSettingsSectionProps["testing"];
  testResult: LlmSettingsSectionProps["testResult"];
  onRemove: LlmSettingsSectionProps["onRemove"];
  removing: LlmSettingsSectionProps["removing"];
  classifierMix: LlmSettingsSectionProps["classifierMix"];
  classifierMixError: LlmSettingsSectionProps["classifierMixError"];
  heuristicNoticeDismissed: LlmSettingsSectionProps["heuristicNoticeDismissed"];
  onDismissHeuristicNotice: LlmSettingsSectionProps["onDismissHeuristicNotice"];

  // usage tab -- same field names LlmUsageSection takes. `usage`/`usageError`
  // double as the ai tab's one-line usage summary, same as the field names
  // already shared between LlmSettingsSectionProps and LlmUsageSectionProps.
  usage: LlmUsageSectionProps["usage"];
  usageError: LlmUsageSectionProps["usageError"];
  days: LlmUsageSectionProps["days"];
  onDaysChange: LlmUsageSectionProps["onDaysChange"];
}

/**
 * The settings dialog: one Radix Dialog with a mono-lowercase tab row over
 * `accounts` / `ai model` / `usage`, replacing the old accounts-popover
 * management UI plus the two standalone AI dialogs (settings-card plan §2).
 *
 * All three tab sections stay mounted for the whole dialog session --
 * inactive ones are hidden via the `hidden` attribute, never conditionally
 * unmounted -- so switching ai → usage → ai never discards an unsaved AI-form
 * edit (plan §3.2, "tab mounting & focus rules"). Each section only fully
 * remounts when the dialog itself closes (Radix unmounts `DialogContent`'s
 * children once its close animation settles), which is also what resets
 * `LlmSettingsSection`'s once-per-session hydration flag.
 *
 * On open, focus goes to the ACTIVE tab button, not whatever Radix would
 * otherwise pick (the first tabbable element, always the `accounts` tab) --
 * otherwise a direct-open route like the command palette's "AI usage…" would
 * open on the right tab but focus the wrong button.
 */
export function SettingsDialog({
  open,
  onOpenChange,
  tab,
  onTabChange,
  connections,
  health,
  onConnectGmail,
  onConnectOutlook,
  onDisconnect,
  onConnectionUpdated,
  settings,
  settingsError,
  onRetrySettings,
  onSave,
  saving,
  onTest,
  testing,
  testResult,
  onRemove,
  removing,
  classifierMix,
  classifierMixError,
  heuristicNoticeDismissed,
  onDismissHeuristicNotice,
  usage,
  usageError,
  days,
  onDaysChange,
}: SettingsDialogProps) {
  const tabRefs = useRef<Record<SettingsTab, HTMLButtonElement | null>>({
    accounts: null,
    ai: null,
    usage: null,
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-2xl bg-[var(--color-panel)] border-border h-[min(34rem,85vh)] flex flex-col"
        onOpenAutoFocus={(e) => {
          // Radix's default is the first tabbable element, which is always
          // the `accounts` tab button -- wrong whenever a route opens ai/usage
          // directly.
          e.preventDefault();
          tabRefs.current[tab]?.focus();
        }}
      >
        <DialogTitle className="font-mono text-[11px] tracking-wide text-muted-foreground mb-1 font-normal">
          settings
        </DialogTitle>

        {/* Plain aria-pressed buttons, not role="tablist" -- we don't
            implement the ARIA tabs contract (arrow-key nav, tabpanel roles),
            and announcing half of it misleads screen readers worse than the
            toggle-button semantics we actually have. Same pattern as the
            usage range buttons. */}
        <div className="flex items-center gap-1 shrink-0">
          {TABS.map((t) => (
            <button
              key={t.key}
              ref={(el) => {
                tabRefs.current[t.key] = el;
              }}
              type="button"
              aria-pressed={tab === t.key}
              onClick={() => onTabChange(t.key)}
              className={cn(
                "h-6 px-2.5 rounded border text-[11px] font-mono cursor-pointer transition-colors",
                tab === t.key
                  ? "border-primary bg-primary text-primary-foreground font-medium"
                  : "border-border bg-[var(--color-panel-hi)] text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Fixed dialog height above; each tab scrolls internally so a tab
            switch never resizes the dialog. */}
        <div className="flex-1 min-h-0 border-t border-border pt-2.5">
          <div hidden={tab !== "accounts"} className="h-full overflow-y-auto pr-0.5">
            <AccountsSection
              connections={connections}
              health={health}
              onConnectGmail={onConnectGmail}
              onConnectOutlook={onConnectOutlook}
              onDisconnect={onDisconnect}
              onConnectionUpdated={onConnectionUpdated}
            />
          </div>
          <div hidden={tab !== "ai"} className="h-full overflow-y-auto pr-0.5">
            <LlmSettingsSection
              open={open}
              settings={settings}
              settingsError={settingsError}
              onRetrySettings={onRetrySettings}
              onSave={onSave}
              saving={saving}
              onTest={onTest}
              testing={testing}
              testResult={testResult}
              onRemove={onRemove}
              removing={removing}
              usage={usage}
              usageError={usageError}
              onOpenUsage={() => onTabChange("usage")}
              classifierMix={classifierMix}
              classifierMixError={classifierMixError}
              heuristicNoticeDismissed={heuristicNoticeDismissed}
              onDismissHeuristicNotice={onDismissHeuristicNotice}
            />
          </div>
          <div hidden={tab !== "usage"} className="h-full overflow-y-auto pr-0.5">
            <LlmUsageSection
              usage={usage}
              usageError={usageError}
              days={days}
              onDaysChange={onDaysChange}
            />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
