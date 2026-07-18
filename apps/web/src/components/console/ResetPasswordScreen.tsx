import { useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { ApiError, resetPassword, setToken } from "@/lib/api";
import type { User } from "@/lib/types";
import { Mark } from "./Mark";

interface Props {
  onAuthed: (user: User) => void;
}

function readAndScrubToken(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  window.history.replaceState({}, "", window.location.pathname);
  return token;
}

export function ResetPasswordScreen({ onAuthed }: Props) {
  const [token] = useState(readAndScrubToken);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState(!token);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    if (newPassword.length < 8 || newPassword.length > 128) {
      setErr("password must be 8 to 128 characters");
      return;
    }
    if (newPassword !== confirmPassword) {
      setErr("passwords do not match");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      const res = await resetPassword(token, newPassword);
      setToken(res.access_token);
      window.history.replaceState({}, "", "/");
      onAuthed(res.user);
      toast.success("password reset — signed in");
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        setInvalid(true);
      } else {
        setErr((e as Error).message || "could not reset password");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative min-h-screen overflow-hidden flex items-center justify-center px-4 bg-background">
      <div
        aria-hidden="true"
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "radial-gradient(ellipse 640px 480px at 50% 38%, color-mix(in oklab, var(--primary) 12%, transparent), transparent 70%)",
        }}
      />
      <div className="relative z-10 w-full max-w-sm rounded-lg border border-border bg-[var(--color-panel)] p-6 shadow-xl">
        <div className="flex items-center gap-3 mb-1.5">
          <div className="h-10 w-10 rounded bg-primary/15 border border-primary/40 flex items-center justify-center phosphor text-primary">
            <Mark className="h-8 w-8" />
          </div>
          <h1 className="font-mono text-2xl font-semibold tracking-tight">
            CortexMail
          </h1>
        </div>
        <p className="text-[10.5px] tracking-tight text-muted-foreground mb-5 font-mono">
          your inbox, triaged by Cortex
        </p>
        {invalid ? (
          <div className="font-mono">
            <p className="text-sm text-foreground">This link is invalid or has expired.</p>
            <button
              type="button"
              onClick={() => (window.location.href = "/")}
              className="mt-5 text-[11px] text-primary hover:brightness-110 cursor-pointer"
            >
              request a new link
            </button>
          </div>
        ) : (
          <form onSubmit={submit}>
            <label
              htmlFor="reset-password"
              className="block text-[11px] tracking-wide text-muted-foreground font-mono mb-1.5"
            >
              new password
            </label>
            <input
              id="reset-password"
              autoFocus
              type="password"
              required
              minLength={8}
              maxLength={128}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="w-full h-9 px-2.5 rounded border border-border bg-background text-[13px] font-mono outline-none focus:border-primary"
            />
            <label
              htmlFor="reset-confirm-password"
              className="block text-[11px] tracking-wide text-muted-foreground font-mono mt-3 mb-1.5"
            >
              confirm new password
            </label>
            <input
              id="reset-confirm-password"
              type="password"
              required
              minLength={8}
              maxLength={128}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full h-9 px-2.5 rounded border border-border bg-background text-[13px] font-mono outline-none focus:border-primary"
            />
            {err && (
              <div role="alert" className="mt-3 text-xs text-destructive font-mono">
                {err}
              </div>
            )}
            <button
              type="submit"
              disabled={busy}
              className="mt-4 w-full h-9 rounded bg-primary text-primary-foreground font-mono text-[13px] font-semibold flex items-center justify-center gap-2 cursor-pointer transition-[filter] hover:brightness-110 disabled:opacity-50 disabled:cursor-default"
            >
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {busy ? "resetting…" : "Reset password & sign in"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
