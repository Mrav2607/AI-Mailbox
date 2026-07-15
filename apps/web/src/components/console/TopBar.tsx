import { useState } from "react";
import {
  Loader2,
  Download,
  Sparkles,
  LogOut,
  Columns3,
  Monitor,
  Moon,
  Sun,
} from "lucide-react";
import { Mark } from "./Mark";
import { Popover } from "./Popover";
import { LayoutPicker } from "./LayoutPicker";
import { bucketLabel } from "@/lib/labels";
import { BUCKETS } from "@/lib/types";
import type { Arrangement } from "@/lib/layout";
import { THEME_PREFS } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";
import { AUTO_SYNC_CHOICES } from "@/lib/use-auto-sync";
import type {
  BackfillOptions,
  BucketKey,
  ClassifierBackend,
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
  theme: ThemePref;
  onTheme: (t: ThemePref) => void;
  autoSync: number;
  onAutoSync: (s: number) => void;
}

const THEME_ICONS: Record<ThemePref, typeof Sun> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

const BACKENDS: { value: ClassifierBackend; label: string }[] = [
  { value: "local", label: "local encoder" },
  { value: "gemini", label: "gemini (LLM)" },
  { value: "heuristic", label: "heuristic" },
];

function clamp(n: number, lo: number, hi: number): number {
  if (Number.isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="px-2.5 py-1 rounded border border-border bg-[var(--color-panel)] flex items-baseline gap-1.5 font-mono">
      <span className="text-[10.5px] text-muted-foreground">{label}</span>
      <span className="text-[12.5px] tabular-nums">{value}</span>
    </div>
  );
}

const fieldLabel = "font-mono text-[11px] text-muted-foreground";
const control =
  "w-full bg-[var(--color-panel)] border border-border rounded px-2 py-1 text-[12px] font-mono text-foreground focus-visible:outline-none focus-visible:border-primary/60";

function IngestForm({
  busy,
  onSubmit,
  autoSync,
  onAutoSync,
}: {
  busy: boolean;
  onSubmit: (o: IngestOptions) => void;
  autoSync: number;
  onAutoSync: (s: number) => void;
}) {
  // String state so a mid-edit (cleared) field never becomes NaN; we parse
  // and clamp on submit instead.
  const [count, setCount] = useState("100");
  const [classify, setClassify] = useState(true);
  const [refreshExisting, setRefreshExisting] = useState(false);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const n = parseInt(count, 10);
        onSubmit({
          maxResults: clamp(Number.isNaN(n) ? 100 : n, 1, 500),
          classify,
          refreshExisting,
        });
      }}
      className="space-y-2.5"
    >
      <div className={fieldLabel}>ingest gmail</div>
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
      <button
        type="submit"
        disabled={busy}
        className="w-full h-7 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
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
}: {
  busy: boolean;
  currentBucket: BucketKey;
  onSubmit: (o: BackfillOptions) => void;
}) {
  // Same string-state trick as IngestForm: parse/clamp on submit so a cleared
  // field never submits NaN.
  const [limit, setLimit] = useState("200");
  const [bucket, setBucket] = useState<BucketKey>(
    currentBucket === "done" ? "all" : currentBucket,
  );
  const [backend, setBackend] = useState<ClassifierBackend>("local");
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
          {/* No "done" here: backfill scopes by classification, not done-ness. */}
          {BUCKETS.filter((b) => b !== "done").map((b) => (
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
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </select>
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
        className="w-full h-7 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
      >
        run backfill
      </button>
    </form>
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
  theme,
  onTheme,
  autoSync,
  onAutoSync,
}: Props) {
  const s = overview?.summary;
  const ThemeIcon = THEME_ICONS[theme];
  const nextTheme =
    THEME_PREFS[(THEME_PREFS.indexOf(theme) + 1) % THEME_PREFS.length];
  return (
    <header className="h-11 shrink-0 border-b border-border bg-[var(--color-panel)] panel-lift flex items-center gap-3 px-3">
      <div className="flex items-center gap-2 mr-1">
        <div className="h-5 w-5 rounded bg-primary/15 border border-primary/40 flex items-center justify-center phosphor text-primary">
          <Mark className="h-3.5 w-3.5" />
        </div>
        <span className="font-mono text-[13px] font-semibold tracking-tight">
          AI&nbsp;Mailbox
        </span>
        <span className="text-[10px] font-mono text-muted-foreground border border-border rounded px-1 py-0.5">
          console
        </span>
      </div>

      {/* Stats and the email are the first things to go on narrow windows —
          the action buttons matter more than the vanity row. */}
      <div className="hidden md:flex items-center gap-1.5">
        <Stat label="threads" value={s?.threads ?? "—"} />
        <Stat label="msgs" value={s?.messages ?? "—"} />
        <Stat label="classified" value={s?.classified ?? "—"} />
      </div>

      <div className="flex-1" />

      <div data-tour="topbar-sync" className="flex items-center gap-3">
        <Popover
          open={ingestOpen}
          onOpenChange={onIngestOpenChange}
          trigger={
            <button
              onClick={() => onIngestOpenChange(!ingestOpen)}
              disabled={ingesting}
              aria-expanded={ingestOpen}
              className="h-7 px-2.5 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {ingesting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Download className="h-3 w-3" />
              )}
              ingest
            </button>
          }
        >
          <IngestForm
            busy={ingesting}
            onSubmit={(o) => {
              onIngestOpenChange(false);
              onIngest(o);
            }}
            autoSync={autoSync}
            onAutoSync={onAutoSync}
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
              className="h-7 px-2.5 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {backfilling ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Sparkles className="h-3 w-3" />
              )}
              backfill
            </button>
          }
        >
          <BackfillForm
            busy={backfilling}
            currentBucket={currentBucket}
            onSubmit={(o) => {
              onBackfillOpenChange(false);
              onBackfill(o);
            }}
          />
        </Popover>
      </div>

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
        <LayoutPicker arrangement={arrangement} onArrangement={onArrangement} />
      </Popover>

      <button
        data-tour="theme"
        onClick={() => onTheme(nextTheme)}
        aria-label={`Theme: ${theme}. Switch to ${nextTheme}.`}
        title={`theme: ${theme} → ${nextTheme}`}
        className="h-7 w-7 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
      >
        <ThemeIcon className="h-3 w-3" />
      </button>

      <div className="mx-1 h-5 w-px bg-border" />

      <span className="hidden md:inline text-[11.5px] font-mono text-muted-foreground truncate max-w-[180px]">
        {user?.email ?? "—"}
      </span>
      <button
        onClick={onLogout}
        // "everywhere" because this revokes the session server-side, not just in
        // this browser. Don't shorten it back to "sign out" -- the label has to
        // match what the button actually does.
        title="sign out everywhere"
        aria-label="Sign out everywhere"
        className="h-7 w-7 rounded border border-border hover:bg-accent hover:text-foreground flex items-center justify-center text-muted-foreground cursor-pointer transition-colors"
      >
        <LogOut className="h-3 w-3" />
      </button>
    </header>
  );
}
