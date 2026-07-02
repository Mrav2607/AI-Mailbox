import { Loader2, Download, Sparkles, LogOut } from "lucide-react";
import type { Overview, User } from "@/lib/types";

interface Props {
  user: User | null;
  overview: Overview | null;
  ingesting: boolean;
  backfilling: boolean;
  onIngest: () => void;
  onBackfill: () => void;
  onLogout: () => void;
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="px-2.5 py-1 rounded border border-border bg-[var(--color-panel)] flex items-baseline gap-1.5 font-mono">
      <span className="text-[10.5px] text-muted-foreground">{label}</span>
      <span className="text-[12.5px] tabular-nums">{value}</span>
    </div>
  );
}

export function TopBar({
  user,
  overview,
  ingesting,
  backfilling,
  onIngest,
  onBackfill,
  onLogout,
}: Props) {
  const s = overview?.summary;
  return (
    <header className="h-11 shrink-0 border-b border-border bg-[var(--color-panel)] flex items-center gap-3 px-3">
      <div className="flex items-center gap-2 mr-1">
        <div className="h-5 w-5 rounded bg-primary/15 border border-primary/40 flex items-center justify-center phosphor">
          <Sparkles className="h-3 w-3 text-primary" />
        </div>
        <span className="font-mono text-[13px] font-semibold tracking-tight">
          AI&nbsp;Mailbox
        </span>
        <span className="text-[10px] font-mono text-muted-foreground border border-border rounded px-1 py-0.5">
          console
        </span>
      </div>

      <div className="flex items-center gap-1.5">
        <Stat label="threads" value={s?.threads ?? "—"} />
        <Stat label="msgs" value={s?.messages ?? "—"} />
        <Stat label="classified" value={s?.classified ?? "—"} />
      </div>

      <div className="flex-1" />

      <button
        onClick={onIngest}
        disabled={ingesting}
        className="h-7 px-2.5 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent flex items-center gap-1.5 text-[12px] font-mono disabled:opacity-50"
      >
        {ingesting ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Download className="h-3 w-3" />
        )}
        ingest
      </button>
      <button
        onClick={onBackfill}
        disabled={backfilling}
        className="h-7 px-2.5 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary flex items-center gap-1.5 text-[12px] font-mono disabled:opacity-50"
      >
        {backfilling ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Sparkles className="h-3 w-3" />
        )}
        backfill
      </button>

      <div className="mx-1 h-5 w-px bg-border" />

      <span className="text-[11.5px] font-mono text-muted-foreground truncate max-w-[180px]">
        {user?.email ?? "—"}
      </span>
      <button
        onClick={onLogout}
        title="sign out"
        className="h-7 w-7 rounded border border-border hover:bg-accent flex items-center justify-center text-muted-foreground"
      >
        <LogOut className="h-3 w-3" />
      </button>
    </header>
  );
}
