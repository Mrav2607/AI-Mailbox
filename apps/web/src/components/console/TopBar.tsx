import { useEffect, useState } from "react";
import {
  BrainCircuit,
  Loader2,
  Download,
  Mail,
  MessagesSquare,
  Settings,
  Sparkles,
  LogOut,
  Columns3,
  Monitor,
  Moon,
  Sun,
} from "lucide-react";
import { GoogleMark } from "./GoogleMark";
import { MicrosoftMark } from "./MicrosoftMark";
import { Mark } from "./Mark";
import { Popover } from "./Popover";
import { LayoutPicker } from "./LayoutPicker";
import { bucketLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { fieldLabel, control } from "@/lib/ui";
import { BUCKETS } from "@/lib/types";
import type { Arrangement, Density } from "@/lib/layout";
import { THEME_PREFS } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";
import { AUTO_SYNC_CHOICES } from "@/lib/use-auto-sync";
import type { AccountSyncHealth, SyncHealth } from "@/lib/api";
import type {
  BackfillOptions,
  BucketKey,
  ClassifierBackend,
  Connection,
  IngestOptions,
  Overview,
  User,
} from "@/lib/types";

interface Props {
  user: User | null;
  overview: Overview | null;
  ingesting: boolean;
  backfilling: boolean;
  currentBucket: BucketKey;
  onIngest: (opts: IngestOptions) => void;
  onBackfill: (opts: BackfillOptions) => void;
  onLogout: () => void;
  ingestOpen: boolean;
  onIngestOpenChange: (v: boolean) => void;
  backfillOpen: boolean;
  onBackfillOpenChange: (v: boolean) => void;
  layoutOpen: boolean;
  onLayoutOpenChange: (v: boolean) => void;
  arrangement: Arrangement;
  onArrangement: (a: Arrangement) => void;
  density?: Density;
  onDensity?: (d: Density) => void;
  fontScale?: number;
  onFontScale?: (s: number) => void;
  theme: ThemePref;
  onTheme: (t: ThemePref) => void;
  autoSync: number;
  onAutoSync: (s: number) => void;
  connections: Connection[];
  health: SyncHealth | null;
  accountsOpen: boolean;
  onAccountsOpenChange: (v: boolean) => void;
  // Opens the settings dialog at the accounts tab -- the popover itself is
  // read-only now, connect/disconnect/label-sync management all live there
  // (settings-card plan §3.1).
  onOpenSettings: () => void;
  // Opens the settings dialog at the ai tab -- only the backfill form's
  // inline link needs this one; the popover's own AI-settings button is
  // gone.
  onOpenAiSettings: () => void;
  // Would picking the "llm" backend actually reach an LLM? null while the
  // settings fetch is in flight -- an unloaded fetch must not disable the
  // option, since "we don't know yet" isn't "you can't".
  llmUsable: boolean | null;
  // Set while the tour is anchored to the matching popover — user-originated
  // closes are ignored until the tour itself moves on.
  ingestLocked?: boolean;
  accountsLocked?: boolean;
}

const THEME_ICONS: Record<ThemePref, typeof Sun> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

const BACKENDS: { value: ClassifierBackend; label: string }[] = [
  { value: "local", label: "local encoder" },
  { value: "llm", label: "LLM" },
  { value: "heuristic", label: "heuristic" },
];

function clamp(n: number, lo: number, hi: number): number {
  if (Number.isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

function Stat({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Mail;
  label: string;
  value: number | string;
}) {
  return (
    <div className="px-2.5 py-1 rounded border border-border bg-[var(--color-panel)] flex items-center gap-1.5 font-mono">
      <Icon className="h-3 w-3 text-muted-foreground/80 shrink-0" />
      <span className="text-[10.5px] text-muted-foreground">{label}</span>
      <span className="text-[12.5px] tabular-nums">{value}</span>
    </div>
  );
}

function IngestForm({
  busy,
  onSubmit,
  autoSync,
  onAutoSync,
  connections,
}: {
  busy: boolean;
  onSubmit: (o: IngestOptions) => void;
  autoSync: number;
  onAutoSync: (s: number) => void;
  connections: Connection[];
}) {
  // String state so a mid-edit (cleared) field never becomes NaN; we parse
  // and clamp on submit instead.
  const [count, setCount] = useState("100");
  const [classify, setClassify] = useState(true);
  const [refreshExisting, setRefreshExisting] = useState(false);
  // null = "all eligible accounts" — an account connected after this dialog
  // was last touched stays included by default instead of silently skipped.
  const [checked, setChecked] = useState<Set<string> | null>(null);

  const eligible = connections.filter((c) => !c.reauth_required);
  const effective = checked ?? new Set(eligible.map((c) => c.id));
  const showPicker = connections.length > 1;
  const noneSelected = showPicker && effective.size === 0;

  const toggleAccount = (id: string) => {
    setChecked((prev) => {
      const next = new Set(prev ?? eligible.map((c) => c.id));
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const n = parseInt(count, 10);
        const allEligibleSelected = eligible.every((c) => effective.has(c.id));
        onSubmit({
          maxResults: clamp(Number.isNaN(n) ? 100 : n, 1, 500),
          classify,
          refreshExisting,
          // Selecting every eligible account is the same as not filtering —
          // omit the param so the request shape matches the common case.
          accountIds: showPicker && !allEligibleSelected ? [...effective] : undefined,
        });
      }}
      className="space-y-2.5"
    >
      <div className={fieldLabel}>ingest mail</div>
      <label className="block space-y-1">
        <span className={fieldLabel}>how many threads (1–500)</span>
        <input
          type="number"
          min={1}
          max={500}
          value={count}
          onChange={(e) => setCount(e.target.value)}
          className={control}
        />
      </label>
      <label className="flex items-center gap-2 cursor-pointer text-[12px] font-mono text-foreground/85">
        <input
          type="checkbox"
          checked={classify}
          onChange={(e) => setClassify(e.target.checked)}
          className="accent-primary"
        />
        classify on ingest
      </label>
      <label className="flex items-center gap-2 cursor-pointer text-[12px] font-mono text-foreground/85">
        <input
          type="checkbox"
          checked={refreshExisting}
          onChange={(e) => setRefreshExisting(e.target.checked)}
          className="accent-primary"
        />
        re-fetch existing threads
      </label>
      {showPicker && (
        <div className="space-y-1 pt-2 border-t border-border">
          <span className={fieldLabel}>accounts</span>
          {connections.map((c) => (
            <label
              key={c.id}
              className={cn(
                "flex items-center gap-2 text-[12px] font-mono",
                c.reauth_required
                  ? "text-muted-foreground/50 cursor-not-allowed"
                  : "text-foreground/85 cursor-pointer",
              )}
            >
              <input
                type="checkbox"
                checked={!c.reauth_required && effective.has(c.id)}
                disabled={c.reauth_required}
                onChange={() => toggleAccount(c.id)}
                className="accent-primary"
              />
              <span className="truncate">{c.email_address}</span>
            </label>
          ))}
          {connections.some((c) => c.reauth_required) && (
            <p className="text-[10.5px] text-muted-foreground font-mono leading-snug">
              reauth required — reconnect to sync
            </p>
          )}
        </div>
      )}
      <button
        type="submit"
        disabled={busy || noneSelected}
        className="w-full h-7 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary-tint-foreground text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
      >
        run ingest
      </button>
      {/* Lives with the ingest controls but applies immediately, no submit. */}
      <label className="block space-y-1 pt-2 border-t border-border">
        <span className={fieldLabel}>auto-sync</span>
        <select
          value={autoSync}
          onChange={(e) => onAutoSync(Number(e.target.value))}
          className={control}
        >
          {AUTO_SYNC_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
          {!AUTO_SYNC_CHOICES.some((c) => c.value === autoSync) && (
            <option value={autoSync}>{autoSync}s (custom)</option>
          )}
        </select>
      </label>
    </form>
  );
}

function BackfillForm({
  busy,
  currentBucket,
  onSubmit,
  llmUsable,
  onOpenLlmSettings,
}: {
  busy: boolean;
  currentBucket: BucketKey;
  onSubmit: (o: BackfillOptions) => void;
  llmUsable: boolean | null;
  onOpenLlmSettings: () => void;
}) {
  // Same string-state trick as IngestForm: parse/clamp on submit so a cleared
  // field never submits NaN.
  const [limit, setLimit] = useState("200");
  // Backfill scopes by classification state, not lifecycle state -- both
  // lifecycle buckets (done, snoozed) map to "all" (docs/plans/2026-08-13-
  // snooze-plan.md §3.6/P2-4), and the force/labeled state below derives
  // from that mapped value, not from currentBucket directly.
  const [bucket, setBucket] = useState<BucketKey>(
    currentBucket === "done" || currentBucket === "snoozed" ? "all" : currentBucket,
  );
  const [backend, setBackend] = useState<ClassifierBackend>("local");
  // Only false disables -- null means the settings fetch hasn't landed.
  const llmBlocked = llmUsable === false;
  // If settings land after the user already picked LLM, snap back rather than
  // submit a run that would silently classify with keyword rules instead.
  useEffect(() => {
    if (llmBlocked && backend === "llm") setBackend("local");
  }, [llmBlocked, backend]);
  const labeled = bucket !== "unclassified" && bucket !== "all";
  // A labeled bucket is already classified, so re-running it needs force.
  const [force, setForce] = useState(labeled);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const n = parseInt(limit, 10);
        onSubmit({ limit: clamp(Number.isNaN(n) ? 200 : n, 1, 500), bucket, backend, force });
      }}
      className="space-y-2.5"
    >
      <div className={fieldLabel}>classify / backfill</div>
      <label className="block space-y-1">
        <span className={fieldLabel}>bucket</span>
        <select
          value={bucket}
          onChange={(e) => {
            const b = e.target.value as BucketKey;
            setBucket(b);
            if (b !== "unclassified" && b !== "all") setForce(true);
          }}
          className={control}
        >
          {/* No "done"/"snoozed" here: backfill scopes by classification,
              not lifecycle state. */}
          {BUCKETS.filter((b) => b !== "done" && b !== "snoozed").map((b) => (
            <option key={b} value={b}>
              {bucketLabel(b)}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1">
        <span className={fieldLabel}>model</span>
        <select
          value={backend}
          onChange={(e) => setBackend(e.target.value as ClassifierBackend)}
          className={control}
        >
          {BACKENDS.map((b) => (
            <option
              key={b.value}
              value={b.value}
              disabled={b.value === "llm" && llmBlocked}
            >
              {b.value === "llm" && llmBlocked ? "LLM (needs your key)" : b.label}
            </option>
          ))}
        </select>
        {llmBlocked && (
          <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
            The LLM needs a key. Add yours and turn on mail sorting in{" "}
            <button
              type="button"
              onClick={onOpenLlmSettings}
              className="text-primary-tint-foreground underline underline-offset-2 cursor-pointer"
            >
              AI settings
            </button>
            .
          </p>
        )}
      </label>
      <label className="block space-y-1">
        <span className={fieldLabel}>how many (1–500)</span>
        <input
          type="number"
          min={1}
          max={500}
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
          className={control}
        />
      </label>
      <label className="flex items-center gap-2 cursor-pointer text-[12px] font-mono text-foreground/85">
        <input
          type="checkbox"
          checked={force}
          onChange={(e) => setForce(e.target.checked)}
          className="accent-primary"
        />
        force re-classify
      </label>
      {labeled && !force && (
        <p className="text-[10.5px] text-muted-foreground font-mono leading-snug">
          this bucket is already classified — enable force to re-run it.
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="w-full h-7 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary-tint-foreground text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
      >
        run backfill
      </button>
    </form>
  );
}

type AccountStatus = "ok" | "amber" | "red";

const ACCOUNT_STATUS_DOT: Record<AccountStatus, string> = {
  // Filled circle -- syncing fine.
  ok: "rounded-full bg-[var(--success)]",
  // Same circle, hollow instead of filled -- "stale/syncing" reads as a
  // fill-vs-outline difference from "ok", not just a hue difference, so it
  // still tells apart under red/green colorblindness or on a grayscale
  // screen.
  amber: "rounded-full border border-[var(--primary)] bg-transparent",
  // Diamond, not a circle -- the most severe state (reauth) gets its own
  // silhouette on top of the color change.
  red: "rotate-45 bg-destructive",
};

const ACCOUNT_STATUS_LABEL: Record<AccountStatus, string> = {
  ok: "syncing fine",
  amber: "stale or syncing",
  red: "reauth required",
};

function accountStatus(
  connection: Connection,
  accounts: AccountSyncHealth[],
): AccountStatus {
  const match = accounts.find((a) => a.provider_account_id === connection.id);
  if (connection.reauth_required || match?.reason === "reauth_required") return "red";
  if (!match || match.stale || match.sync_in_progress) return "amber";
  return "ok";
}

// One read-only line under each account (settings-card plan §3.1) --
// reauth takes precedence over everything else, since an enabled-but-paused
// account must not read "synced" just because label_sync_enabled is true.
function labelSyncStatusLine(connection: Connection): string {
  if (connection.label_sync_enabled && connection.reauth_required) {
    return "labels paused — reconnect";
  }
  if (!connection.label_sync_enabled) return "labels off";
  const drift = connection.label_sync_drift ?? 0;
  if (drift > 0) return `labels syncing — ${drift} remaining`;
  return "labels synced";
}

function AccountsMenu({
  connections,
  health,
  onOpenSettings,
  actionsLocked,
}: {
  connections: Connection[];
  health: SyncHealth | null;
  onOpenSettings: () => void;
  // Mirrors the popover's own lockOpen: while the tour is anchored here, the
  // settings button must not stack a modal underneath it.
  actionsLocked?: boolean;
}) {
  const accounts = health?.accounts ?? [];
  return (
    <div className="flex flex-col max-h-[min(60vh,calc(100vh-8rem))] space-y-2.5">
      <div className={fieldLabel}>connected accounts</div>
      {connections.length === 0 ? (
        <p className="text-[11px] text-muted-foreground font-mono">
          no accounts connected
        </p>
      ) : (
        <ul className="min-h-0 flex-1 overflow-y-auto space-y-1.5">
          {connections.map((c) => {
            const status = accountStatus(c, accounts);
            return (
              <li
                key={c.id}
                className="rounded border border-border px-2 py-1.5 space-y-1"
              >
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
                </div>
                <p className="pl-3.5 text-[10px] font-mono text-muted-foreground leading-snug">
                  {labelSyncStatusLine(c)}
                </p>
              </li>
            );
          })}
        </ul>
      )}
      <button
        type="button"
        data-tour="settings"
        disabled={actionsLocked}
        onClick={onOpenSettings}
        className="w-full h-7 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent text-[12px] font-mono cursor-pointer transition-colors"
      >
        settings
      </button>
    </div>
  );
}

export function TopBar({
  user,
  overview,
  ingesting,
  backfilling,
  currentBucket,
  onIngest,
  onBackfill,
  onLogout,
  ingestOpen,
  onIngestOpenChange,
  backfillOpen,
  onBackfillOpenChange,
  layoutOpen,
  onLayoutOpenChange,
  arrangement,
  onArrangement,
  density,
  onDensity,
  fontScale,
  onFontScale,
  theme,
  onTheme,
  autoSync,
  onAutoSync,
  connections,
  health,
  accountsOpen,
  onAccountsOpenChange,
  onOpenSettings,
  onOpenAiSettings,
  llmUsable,
  ingestLocked,
  accountsLocked,
}: Props) {
  const s = overview?.summary;
  const ThemeIcon = THEME_ICONS[theme];
  const nextTheme =
    THEME_PREFS[(THEME_PREFS.indexOf(theme) + 1) % THEME_PREFS.length];
  // User-originated closes are ignored while the tour anchors to this popover;
  // the tour itself closes it through App's raw setters, which bypass this.
  const guardedIngestOpenChange = (v: boolean) => {
    if (ingestLocked && !v) return;
    onIngestOpenChange(v);
  };
  const guardedAccountsOpenChange = (v: boolean) => {
    if (accountsLocked && !v) return;
    onAccountsOpenChange(v);
  };
  return (
    <header className="h-11 shrink-0 border-b border-border bg-[var(--color-panel)] panel-lift flex items-center gap-3 px-3">
      <div className="flex items-center gap-2 mr-1">
        <Mark className="h-5 w-5 shrink-0" />
        <span className="max-[360px]:hidden font-mono text-[13px] font-semibold tracking-tight">
          CortexMail
        </span>
        <span className="hidden md:inline text-[10px] font-mono text-muted-foreground border border-border rounded px-1 py-0.5">
          console
        </span>
      </div>

      {/* Stats and the email are the first things to go on narrow windows —
          the action buttons matter more than the vanity row. */}
      <div className="hidden md:flex items-center gap-1.5">
        <Stat icon={MessagesSquare} label="threads" value={s?.threads ?? "—"} />
        <Stat icon={Mail} label="msgs" value={s?.messages ?? "—"} />
        <Stat icon={BrainCircuit} label="classified" value={s?.classified ?? "—"} />
      </div>

      <div className="flex-1" />

      <div data-tour="topbar-sync" className="flex items-center gap-3">
        <Popover
          open={ingestOpen}
          onOpenChange={guardedIngestOpenChange}
          panelTour="ingest-panel"
          lockOpen={ingestLocked}
          trigger={
            <button
              onClick={() => guardedIngestOpenChange(!ingestOpen)}
              disabled={ingesting}
              aria-expanded={ingestOpen}
              aria-label="Ingest mail"
              className="h-7 max-md:h-10 px-2.5 max-md:w-10 max-md:px-0 max-md:justify-center rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {ingesting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Download className="h-3 w-3" />
              )}
              <span className="max-md:hidden">ingest</span>
            </button>
          }
        >
          <IngestForm
            busy={ingesting}
            onSubmit={(o) => {
              guardedIngestOpenChange(false);
              onIngest(o);
            }}
            autoSync={autoSync}
            onAutoSync={onAutoSync}
            connections={connections}
          />
        </Popover>

        <Popover
          open={backfillOpen}
          onOpenChange={onBackfillOpenChange}
          trigger={
            <button
              onClick={() => onBackfillOpenChange(!backfillOpen)}
              disabled={backfilling}
              aria-expanded={backfillOpen}
              aria-label="Classify / backfill"
              className="h-7 max-md:h-10 px-2.5 max-md:w-10 max-md:px-0 max-md:justify-center rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary-tint-foreground flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {backfilling ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Sparkles className="h-3 w-3" />
              )}
              <span className="max-md:hidden">backfill</span>
            </button>
          }
        >
          <BackfillForm
            busy={backfilling}
            currentBucket={currentBucket}
            llmUsable={llmUsable}
            onOpenLlmSettings={() => {
              onBackfillOpenChange(false);
              onOpenAiSettings();
            }}
            onSubmit={(o) => {
              onBackfillOpenChange(false);
              onBackfill(o);
            }}
          />
        </Popover>
      </div>

      <div className="hidden md:block">
        <Popover
          open={layoutOpen}
          onOpenChange={onLayoutOpenChange}
          trigger={
            <button
              data-tour="layout"
              onClick={() => onLayoutOpenChange(!layoutOpen)}
              aria-expanded={layoutOpen}
              className="h-7 px-2.5 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors"
            >
              <Columns3 className="h-3 w-3" />
              layout
            </button>
          }
        >
          <LayoutPicker
            arrangement={arrangement}
            onArrangement={onArrangement}
            density={density}
            onDensity={onDensity}
            fontScale={fontScale}
            onFontScale={onFontScale}
          />
        </Popover>
      </div>

      <button
        data-tour="theme"
        onClick={() => onTheme(nextTheme)}
        aria-label={`Theme: ${theme}. Switch to ${nextTheme}.`}
        title={`theme: ${theme} → ${nextTheme}`}
        className="h-7 w-7 max-md:h-10 max-md:w-10 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
      >
        <ThemeIcon className="h-3 w-3" />
      </button>

      <div className="hidden md:block mx-1 h-5 w-px bg-border" />

      <div className="hidden md:block">
        <Popover
          open={accountsOpen}
          onOpenChange={guardedAccountsOpenChange}
          panelTour="accounts-panel"
          lockOpen={accountsLocked}
          trigger={
            <button
              data-tour="accounts"
              onClick={() => guardedAccountsOpenChange(!accountsOpen)}
              aria-expanded={accountsOpen}
              aria-label="Mail accounts"
              className="h-7 px-2 rounded border border-transparent hover:border-border hover:bg-accent text-[11.5px] font-mono text-muted-foreground hover:text-foreground truncate max-w-[180px] cursor-pointer transition-colors"
            >
              {user?.email ?? "—"}
            </button>
          }
        >
          <AccountsMenu
            connections={connections}
            health={health}
            onOpenSettings={() => {
              guardedAccountsOpenChange(false);
              onOpenSettings();
            }}
            actionsLocked={accountsLocked}
          />
        </Popover>
      </div>
      {/* Narrow viewports hide the accounts popover entirely, which used to
          leave the command palette as the only way into settings on a phone.
          This gear is the direct path instead; same dialog, accounts tab.
          No data-tour here -- the tour anchors to the popover's settings
          button, and duplicating the attribute would break its selector. */}
      <button
        onClick={onOpenSettings}
        aria-label="Settings"
        title="settings"
        className="md:hidden h-10 w-10 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
      >
        <Settings className="h-3 w-3" />
      </button>
      <button
        onClick={onLogout}
        // "everywhere" because this revokes the session server-side, not just in
        // this browser. Don't shorten it back to "sign out" -- the label has to
        // match what the button actually does.
        title="sign out everywhere"
        aria-label="Sign out everywhere"
        className="h-7 w-7 max-md:h-10 max-md:w-10 rounded border border-border hover:bg-accent hover:text-foreground flex items-center justify-center text-muted-foreground cursor-pointer transition-colors"
      >
        <LogOut className="h-3 w-3" />
      </button>
    </header>
  );
}
