import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Mark } from "./Mark";
import { demoLogin, googleAuthStart, setToken, USE_MOCK } from "@/lib/api";
import type { User } from "@/lib/types";

interface Props {
  onAuthed: (user: User) => void;
}

export function LoginScreen({ onAuthed }: Props) {
  const [email, setEmail] = useState("operator@local.dev");
  const [busy, setBusy] = useState(false);
  const [googleBusy, setGoogleBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await demoLogin(email);
      setToken(res.access_token);
      onAuthed(res.user);
    } catch (e) {
      setErr((e as Error).message || "login failed");
    } finally {
      setBusy(false);
    }
  }

  async function google() {
    setErr(null);
    setGoogleBusy(true);
    try {
      const { auth_url } = await googleAuthStart();
      // Leave the SPA for Google's consent screen; control returns via the
      // /auth/google/callback route handled in App.
      window.location.href = auth_url;
    } catch (e) {
      setErr((e as Error).message || "google sign-in unavailable");
      setGoogleBusy(false);
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
      <form
        onSubmit={submit}
        className="relative z-10 w-full max-w-sm rounded-lg border border-border bg-[var(--color-panel)] p-6 shadow-xl"
      >
        <div className="flex items-center gap-3 mb-1.5">
          <div className="h-10 w-10 rounded bg-primary/15 border border-primary/40 flex items-center justify-center phosphor text-primary">
            <Mark className="h-6 w-6" />
          </div>
          <h1 className="font-mono text-2xl font-semibold tracking-tight">
            CortexMail
          </h1>
        </div>
        <p className="text-[10.5px] tracking-tight text-muted-foreground mb-5 font-mono">
          your inbox, triaged by a cortex · sign in to continue
        </p>

        {!USE_MOCK && (
          <>
            <button
              type="button"
              onClick={google}
              disabled={googleBusy || busy}
              className="w-full h-9 rounded border border-border bg-background font-mono text-[13px] font-semibold flex items-center justify-center gap-2 hover:bg-accent cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {googleBusy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <GoogleMark />
              )}
              {googleBusy ? "redirecting…" : "Continue with Google"}
            </button>

            <div className="my-4 flex items-center gap-2 text-[10.5px] tracking-wide text-muted-foreground font-mono">
              <div className="h-px flex-1 bg-border" />
              or dev login
              <div className="h-px flex-1 bg-border" />
            </div>
          </>
        )}

        <label
          htmlFor="login-email"
          className="block text-[11px] tracking-wide text-muted-foreground font-mono mb-1.5"
        >
          email
        </label>
        <input
          id="login-email"
          autoFocus
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="w-full h-9 px-2.5 rounded border border-border bg-background text-[13px] font-mono outline-none focus:border-primary"
        />

        {err && (
          <div role="alert" className="mt-3 text-xs text-destructive font-mono">
            {err}
          </div>
        )}

        {/* Also locked while the Google redirect is in flight, mirroring the
            Google button, so the two sign-in paths can't race each other. */}
        <button
          type="submit"
          disabled={busy || googleBusy}
          className="mt-4 w-full h-9 rounded bg-primary text-primary-foreground font-mono text-[13px] font-semibold flex items-center justify-center gap-2 cursor-pointer transition-[filter] hover:brightness-110 disabled:opacity-50 disabled:cursor-default"
        >
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {busy ? "signing in…" : "demo login"}
        </button>

        {USE_MOCK && (
          <div className="mt-4 text-[11px] text-muted-foreground font-mono leading-relaxed">
            no VITE_API_BASE_URL configured — running with in-memory mock data
            matching the real API shape.
          </div>
        )}
      </form>
    </div>
  );
}

function GoogleMark() {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z"
      />
      <path
        fill="#34A853"
        d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7C7.96 41.07 15.4 46 24 46z"
      />
      <path
        fill="#FBBC05"
        d="M11.69 28.18c-.44-1.32-.69-2.73-.69-4.18s.25-2.86.69-4.18v-5.7H4.34A21.99 21.99 0 0 0 2 24c0 3.55.85 6.91 2.34 9.88l7.35-5.7z"
      />
      <path
        fill="#EA4335"
        d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z"
      />
    </svg>
  );
}
