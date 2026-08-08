import { Check, Copy, Github, Monitor, Moon, Sun } from "lucide-react";
import { useState } from "react";

import { LABEL_META } from "@/lib/labels";
import { THEME_PREFS } from "@/lib/theme";
import type { ThemePref } from "@/lib/theme";
import type { Label } from "@/lib/types";

import { LandingConsoleMockup } from "./LandingConsoleMockup";

const GITHUB_URL = "https://github.com/Mrav2607/AI-Mailbox";

const THEME_ICONS: Record<ThemePref, typeof Sun> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

// The six labels in fixed keyboard order (1-6), same order the console binds
// them to. LABEL_META's keys are already this set -- no separate list to
// keep in sync.
const LABELS = Object.keys(LABEL_META) as Label[];

const HOW_IT_WORKS = [
  {
    step: "01",
    title: "CONNECT",
    body: "Connect multiple Gmail and Outlook accounts; every message lands in one place, sorted into the six labels.",
  },
  {
    step: "02",
    title: "CLASSIFY",
    body: "Every thread gets a label and a confidence score.",
  },
  {
    step: "03",
    title: "TRIAGE",
    body: "Review buckets at keyboard speed; correct labels as you triage.",
  },
];

const FEATURES = [
  {
    title: "Keyboard-first console",
    body: "j/k move, ⌘K palette, 1-9 buckets, 0 agenda",
  },
  {
    title: "Confidence you can see",
    body: "every prediction shows confidence and model version",
  },
  {
    title: "Your model, your keys",
    body: "supports a locally fine-tuned encoder or bring-your-own LLM key; quick-start ships with a heuristic baseline",
  },
  {
    title: "Self-hosted stack",
    body: "FastAPI · Postgres/pgvector · Redis · one docker compose. Self-host your mailbox data and choose which model processes it.",
  },
];

const QUICK_START_COMMANDS = ["cp deploy/local.env.example deploy/.env", "docker compose up --build"];

export function LandingPage(props: {
  onSignIn: () => void;
  theme: ThemePref;
  onTheme: (pref: ThemePref) => void;
}) {
  const { onSignIn, theme, onTheme } = props;
  const ThemeIcon = THEME_ICONS[theme];
  const nextTheme = THEME_PREFS[(THEME_PREFS.indexOf(theme) + 1) % THEME_PREFS.length];
  const [copied, setCopied] = useState(false);
  const [copyAnnouncement, setCopyAnnouncement] = useState("");

  async function copyQuickStart() {
    const text = QUICK_START_COMMANDS.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setCopyAnnouncement("Copied to clipboard.");
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
      setCopyAnnouncement("Couldn't copy. Select the commands and copy them manually.");
    }
  }

  const ghostButton =
    "h-9 px-3 rounded border border-border bg-[var(--panel-hi)] hover:bg-accent flex items-center gap-1.5 text-[12.5px] font-mono cursor-pointer transition-colors";
  const primaryButton =
    "h-10 px-5 rounded bg-primary text-primary-foreground font-mono text-[13px] font-semibold flex items-center justify-center gap-1.5 cursor-pointer transition-[filter] hover:brightness-110 phosphor";
  const secondaryAnchor =
    "h-10 px-5 rounded border border-border bg-[var(--panel-hi)] hover:bg-accent text-foreground/90 font-mono text-[13px] font-semibold flex items-center justify-center gap-1.5 cursor-pointer transition-colors";
  // One orchestrated page-load reveal in the hero; everything else stays still.
  // CSS animations (not class-gated transitions) so content is never stuck
  // hidden, and motion-reduce turns the whole thing off.
  const reveal =
    "animate-in fade-in slide-in-from-bottom-2 duration-700 [animation-fill-mode:both] motion-reduce:animate-none";

  return (
    <div className="min-h-screen w-full bg-background text-foreground">
      <header className="border-b border-border bg-[var(--panel)] panel-lift">
        <nav className="mx-auto flex h-14 max-w-7xl flex-wrap items-center gap-2 px-4">
          <span className="flex items-center gap-2 font-mono text-[14px] font-semibold tracking-tight text-primary">
            <img src="/appicon.svg" alt="" width={48} height={48} className="h-6 w-6" />
            CORTEXMAIL
          </span>
          <div className="flex-1" />
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            aria-label="CortexMail on GitHub"
            className={ghostButton}
          >
            <Github className="h-3.5 w-3.5" />
            <span className="max-[420px]:hidden">GitHub</span>
          </a>
          <button
            type="button"
            onClick={() => onTheme(nextTheme)}
            aria-label={`Theme: ${theme}. Switch to ${nextTheme}.`}
            title={`theme: ${theme} → ${nextTheme}`}
            className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded border border-border bg-[var(--panel-hi)] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ThemeIcon className="h-3.5 w-3.5" />
          </button>
          <button type="button" onClick={onSignIn} className={ghostButton}>
            Sign in
          </button>
        </nav>
      </header>

      <main>
        <section className="mx-auto max-w-7xl px-4 py-14 sm:py-20">
          <div className="mx-auto max-w-2xl text-center">
            <h1
              className={`text-balance font-mono text-[clamp(30px,5vw,52px)] font-semibold leading-[1.12] tracking-tight ${reveal}`}
            >
              Your inbox, triaged by your own model.
            </h1>
            <p
              className={`mt-5 text-pretty text-[15px] leading-relaxed text-muted-foreground sm:text-[16px] ${reveal} [animation-delay:120ms]`}
            >
              Self-hosted email triage: your mail sorted into six buckets you review at
              keyboard speed.
            </p>
            <div
              className={`mt-7 flex flex-wrap items-center justify-center gap-3 ${reveal} [animation-delay:220ms]`}
            >
              <button type="button" onClick={onSignIn} className={primaryButton}>
                Sign in →
              </button>
              <a href="#quick-start" className={secondaryAnchor}>
                Run it yourself ↓
              </a>
            </div>
          </div>
          <div className={`mx-auto mt-12 max-w-3xl ${reveal} [animation-delay:340ms]`}>
            <LandingConsoleMockup />
            <p className="mt-3 text-center text-[11.5px] text-muted-foreground">
              The triage console: every thread labeled, with the model's confidence next to
              it.
            </p>
          </div>
        </section>

        <section aria-labelledby="how-it-works-heading" className="border-t border-border">
          <div className="mx-auto max-w-7xl px-4 py-12">
            <h2
              id="how-it-works-heading"
              className="text-balance text-center font-mono text-[17px] font-semibold tracking-tight"
            >
              How it works
            </h2>
            <div className="mx-auto mt-8 grid max-w-5xl grid-cols-1 gap-8 sm:grid-cols-3">
              {HOW_IT_WORKS.map((step) => (
                <div key={step.step}>
                  <div className="font-mono text-[13px] font-semibold text-primary">
                    {step.step} {step.title}
                  </div>
                  <p className="mt-2 text-[13.5px] leading-relaxed text-foreground/85">
                    {step.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section aria-labelledby="taxonomy-heading" className="border-t border-border">
          <div className="mx-auto max-w-7xl px-4 py-12 text-center">
            <h2 id="taxonomy-heading" className="font-mono text-[13px] font-medium text-foreground/90">
              Six labels. A closed set: the model never invents a seventh.
            </h2>
            <div className="mx-auto mt-5 flex max-w-3xl flex-wrap items-center justify-center gap-2.5">
              {LABELS.map((label) => {
                const meta = LABEL_META[label];
                return (
                  <span
                    key={label}
                    className="flex items-center gap-1.5 rounded border border-border bg-[var(--panel)] px-2 py-1 font-mono text-[11px]"
                  >
                    <span className="kbd">{meta.key}</span>
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
                    <span className={meta.text}>{meta.name}</span>
                  </span>
                );
              })}
            </div>
          </div>
        </section>

        <section aria-labelledby="agenda-heading" className="border-t border-border">
          <div className="mx-auto max-w-7xl px-4 py-12 text-center">
            <h2
              id="agenda-heading"
              className="text-center font-mono text-[17px] font-semibold tracking-tight"
            >
              Agenda
            </h2>
            <p className="mx-auto mt-4 max-w-2xl text-[13.5px] leading-relaxed text-muted-foreground">
              Action items get pulled out of every account you connect and collected into one
              list, sorted by due date. Press <span className="kbd">0</span> in the console to
              jump to it.
            </p>
          </div>
        </section>

        <section aria-labelledby="features-heading" className="border-t border-border">
          <div className="mx-auto max-w-7xl px-4 py-12">
            <h2
              id="features-heading"
              className="text-center font-mono text-[17px] font-semibold tracking-tight"
            >
              Built for the operator
            </h2>
            <div className="mx-auto mt-8 max-w-3xl divide-y divide-border">
              {FEATURES.map((f) => (
                <div key={f.title} className="flex flex-col gap-1 py-3.5 sm:flex-row sm:gap-4">
                  <div className="font-mono text-[13px] font-semibold text-foreground sm:w-56 sm:shrink-0">
                    {f.title}
                  </div>
                  <p className="text-[13px] leading-relaxed text-muted-foreground">{f.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="quick-start" aria-labelledby="quick-start-heading" className="border-t border-border">
          <div className="mx-auto max-w-7xl px-4 py-12">
            <h2
              id="quick-start-heading"
              className="text-center font-mono text-[17px] font-semibold tracking-tight"
            >
              Running in two commands
            </h2>
            <div className="mx-auto mt-6 max-w-lg">
              {/* The code block is a terminal, not a page panel -- it stays
                  dark even on the light "paper terminal" palette, same as
                  the console mockup above. See LandingConsoleMockup for how
                  the `dark` scoping trick works. */}
              <pre className="dark scrollbar-thin overflow-x-auto rounded-lg border border-border bg-[var(--panel)] p-3 font-mono text-[12.5px] leading-relaxed text-foreground/90 panel-lift">
                <code>{QUICK_START_COMMANDS.join("\n")}</code>
              </pre>
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <button type="button" onClick={() => void copyQuickStart()} className={ghostButton}>
                  {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                  {copied ? "copied" : "copy"}
                </button>
                <span role="status" aria-live="polite" className="text-[11px] font-mono text-muted-foreground">
                  {copyAnnouncement}
                </span>
              </div>
              <p className="mt-4 text-[12.5px] leading-relaxed text-muted-foreground">
                open <code className="font-mono text-foreground/85">localhost:8080</code> and use
                the demo login. No Google account needed.
              </p>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto max-w-7xl px-4 py-6 text-center font-mono text-[11.5px] text-muted-foreground">
          CortexMail: self-hosted email triage.
        </div>
      </footer>
    </div>
  );
}
