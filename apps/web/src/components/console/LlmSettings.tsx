import { useEffect, useRef, useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import type { LlmProvider, LlmSettings, LlmTestResult, LlmUsage } from "@/lib/types";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // Null until the first fetch after login lands -- the modal just doesn't
  // render yet rather than show a half-built form.
  settings: LlmSettings | null;
  onSave: (input: {
    provider: LlmProvider;
    // Absent when the user leaves the field blank to keep an existing key --
    // required only when there's nothing stored yet.
    api_key?: string;
    model: string;
    base_url?: string;
    classification_byok?: boolean;
  }) => void;
  saving: boolean;
  onTest: () => void;
  testing: boolean;
  testResult: LlmTestResult | null;
  onRemove: () => void;
  removing: boolean;
  // Null while the fetch is in flight (or hasn't started) and no prior error
  // -- fetched fresh every time this modal opens, since usage keeps moving
  // even when the credential itself hasn't changed.
  usage: LlmUsage | null;
  // True when the usage fetch failed. Kept separate from `usage` being null
  // so the panel can tell "still loading" apart from "unavailable" -- an
  // outage here must never make the rest of this modal look broken.
  usageError: boolean;
}

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
  openrouter: "OpenRouter",
  groq: "Groq",
  mistral: "Mistral",
  custom: "Custom endpoint",
};

// Hints only, shown as the model field's placeholder -- never submitted on
// their own, and cleared the moment the caller types something real.
const MODEL_PLACEHOLDERS: Record<LlmProvider, string> = {
  openai: "e.g. gpt-4o-mini",
  gemini: "e.g. gemini-2.5-flash",
  openrouter: "e.g. openai/gpt-4o-mini",
  groq: "e.g. llama-3.3-70b-versatile",
  mistral: "e.g. mistral-small-latest",
  custom: "the model name your endpoint expects",
};

const PRESET_PROVIDERS: LlmProvider[] = [
  "openai",
  "gemini",
  "openrouter",
  "groq",
  "mistral",
];

// The UI's own words for each stage -- never the internal "classification" /
// "extraction" column values from the usage response.
const STAGE_LABELS: Record<string, string> = {
  classification: "Sorting",
  extraction: "Agenda",
};

// Only providers we're confident actually resolve to a real usage/billing
// page. Deliberately partial: guessing a URL that 404s is worse than no
// link at all, and "custom" has no dashboard we could point to anyway.
// Where a user goes for the actual money. We only report call and token
// counts, so this link is the other half of "Usage, not billing".
//
// OpenAI and OpenRouter get a deep link straight to the usage page. The rest
// point at the console home instead: those products move their billing pages
// around, and a stale deep link that 404s is worse than one extra click. No
// entry for "custom" on purpose -- we have no idea where that endpoint bills.
const USAGE_DASHBOARD_URLS: Partial<Record<LlmProvider, string>> = {
  openai: "https://platform.openai.com/usage",
  openrouter: "https://openrouter.ai/activity",
  gemini: "https://aistudio.google.com/",
  groq: "https://console.groq.com/",
  mistral: "https://console.mistral.ai/",
};

// Maps the /test route's fixed category constants -- never a raw exception
// string -- to plain copy anyone can follow, regardless of first language.
function testErrorMessage(error: string): string {
  if (error === "connection_failed") return "Could not reach the provider.";
  if (error === "timed_out") return "The provider took too long to reply.";
  if (error === "http_401" || error === "http_403") {
    return "The provider rejected this key.";
  }
  if (error.startsWith("http_")) {
    const code = error.slice("http_".length);
    return `The provider returned an error (HTTP ${code}).`;
  }
  if (error === "invalid_response") {
    return "The provider replied in an unexpected format.";
  }
  if (error === "blocked_by_policy") {
    return "This endpoint is not allowed by the server's settings.";
  }
  return "Something went wrong testing this credential.";
}

const fieldLabel = "font-mono text-[11px] text-muted-foreground";
const control =
  "w-full bg-[var(--color-panel)] border border-border rounded px-2 py-1 text-[12px] font-mono text-foreground";

export function LlmSettingsModal({
  open,
  onOpenChange,
  settings,
  onSave,
  saving,
  onTest,
  testing,
  testResult,
  onRemove,
  removing,
  usage,
  usageError,
}: Props) {
  const [provider, setProvider] = useState<LlmProvider>("openai");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [classificationByok, setClassificationByok] = useState(false);

  // A Test while the modal's open refetches `settings` in App -- track the
  // latest copy in a ref (not state) so that refetch doesn't retrigger the
  // reset effect below and clobber whatever the user's mid-typing.
  const settingsRef = useRef(settings);
  settingsRef.current = settings;

  // Reset the form to whatever's currently saved only when the modal opens
  // -- never on a later settings refetch, which would blow away unsaved
  // edits. The key never repopulates, since the server never sends it back.
  useEffect(() => {
    if (!open) return;
    const current = settingsRef.current;
    if (!current) return;
    setProvider(current.provider ?? "openai");
    setModel(current.model ?? "");
    setApiKey("");
    setBaseUrl(current.provider === "custom" ? (current.base_url ?? "") : "");
    setClassificationByok(current.classification_byok);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!settings) return null;

  const providers = settings.custom_endpoints_enabled
    ? [...PRESET_PROVIDERS, "custom" as const]
    : PRESET_PROVIDERS;

  // `required` is satisfied by whitespace, but the submit path trims both of
  // these before they hit the API -- catch that here so an all-spaces value
  // never gets stored as a working model name or endpoint.
  const modelBlank = model.trim() === "";
  const baseUrlBlank = provider === "custom" && baseUrl.trim() === "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md bg-[var(--color-panel)] border-border">
        <DialogTitle className="font-mono text-[11px] tracking-wide text-muted-foreground mb-1 font-normal">
          AI extraction settings
        </DialogTitle>
        <p className="text-[11px] font-mono text-muted-foreground leading-relaxed">
          save your own API key and CortexMail uses it to pull deadlines and
          to-dos out of your mail.
        </p>

        {settings.custom_blocked && (
          <div className="rounded border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[11.5px] font-mono text-destructive leading-snug">
            your custom endpoint isn&apos;t allowed right now, so extraction is
            paused for your account. Fix the address below, or remove this
            credential, to turn it back on.
          </div>
        )}

        {!settings.configured && (
          <p className="text-[11px] font-mono text-muted-foreground leading-snug">
            {settings.fallback_active
              ? "no key saved yet — the server's shared key is covering your extraction for now."
              : "no key saved yet — extraction is off for your account until you add one."}
          </p>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (modelBlank || baseUrlBlank) return;
            // A blank key on an already-configured credential means "keep
            // what's stored" -- only a brand-new credential needs one.
            const keepsExistingKey = apiKey.trim() === "" && settings.configured;
            onSave({
              provider,
              ...(keepsExistingKey ? {} : { api_key: apiKey }),
              model: model.trim(),
              base_url: provider === "custom" ? baseUrl.trim() : undefined,
              classification_byok: provider === "custom" ? false : classificationByok,
            });
          }}
          className="space-y-2.5"
        >
          <label className="block space-y-1">
            <span className={fieldLabel}>provider</span>
            <select
              value={provider}
              onChange={(e) => {
                const p = e.target.value as LlmProvider;
                setProvider(p);
                // A model name from one provider is almost never valid for
                // another -- don't let a stale value slip through.
                setModel("");
                // Classification BYOK is presets-only -- a custom endpoint
                // always saves with the flag cleared server-side, so clear
                // it here too rather than show it checked next to a
                // disabled, about-to-be-ignored control.
                if (p === "custom") setClassificationByok(false);
              }}
              className={control}
            >
              {providers.map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_LABELS[p]}
                </option>
              ))}
            </select>
          </label>

          <label className="block space-y-1">
            <span className={fieldLabel}>model</span>
            <input
              type="text"
              required
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={MODEL_PLACEHOLDERS[provider]}
              className={control}
            />
            {modelBlank && model.length > 0 && (
              <span className="block text-[10.5px] text-destructive font-mono leading-snug">
                model can&apos;t be just spaces
              </span>
            )}
          </label>

          {provider === "custom" && (
            <label className="block space-y-1">
              <span className={fieldLabel}>endpoint URL</span>
              <input
                type="text"
                required
                maxLength={200}
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-endpoint.example.com/v1"
                className={control}
              />
              <span className="block text-[10.5px] text-muted-foreground font-mono leading-snug">
                {settings.private_endpoints_enabled
                  ? "must start with https:// — or http:// / a private address, since this server allows private endpoints"
                  : "must start with https:// and point to a public address"}
              </span>
              {baseUrlBlank && baseUrl.length > 0 && (
                <span className="block text-[10.5px] text-destructive font-mono leading-snug">
                  endpoint URL can&apos;t be just spaces
                </span>
              )}
            </label>
          )}

          <label className="block space-y-1">
            <span className={fieldLabel}>API key</span>
            <input
              type="password"
              autoComplete="new-password"
              required={!settings.configured}
              minLength={8}
              maxLength={512}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={
                settings.configured
                  ? "leave blank to keep your saved key"
                  : "paste your API key"
              }
              className={control}
            />
            <span className="block text-[10.5px] text-muted-foreground font-mono leading-snug">
              {settings.configured
                ? `leave this blank to keep your saved key (currently ending in ••••${settings.key_suffix})`
                : "your key is encrypted, stored, and never shown again after you save it"}
            </span>
          </label>

          {settings.classifier_uses_llm && (
            <div className="space-y-1">
              <label className="flex items-start gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={classificationByok}
                  disabled={provider === "custom"}
                  onChange={(e) => setClassificationByok(e.target.checked)}
                  className="mt-0.5"
                />
                <span className="text-[12px] font-mono text-foreground leading-snug">
                  Also use my key to sort incoming mail
                </span>
              </label>
              <p className="text-[10.5px] font-mono text-muted-foreground leading-snug pl-[18px]">
                {provider === "custom"
                  ? "Not available for a custom endpoint yet -- only the providers above."
                  : "Sorting runs on every email you receive, so it uses far more of your quota than the Agenda does. Off by default."}
              </p>
              {settings.classification_byok && !settings.classification_eligible && (
                <p className="text-[11px] font-mono text-amber-500 leading-snug pl-[18px]">
                  Your key isn&apos;t set up to sort your mail — the built-in rules are being
                  used instead.
                </p>
              )}
              {settings.classification_byok &&
                settings.classification_eligible &&
                (settings.classifier_backend === "local" ||
                  settings.classifier_backend === "auto") && (
                  <p className="text-[10.5px] font-mono text-muted-foreground leading-snug pl-[18px]">
                    The built-in model sorts most mail on its own -- your key is only used
                    when it can&apos;t.
                  </p>
                )}
            </div>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button
              type="submit"
              disabled={saving || modelBlank || baseUrlBlank}
              className="h-7 px-3 rounded border border-primary/50 bg-primary/15 hover:bg-primary/25 text-primary text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {saving ? "saving…" : "save"}
            </button>
            <button
              type="button"
              onClick={onTest}
              disabled={!settings.configured || testing || saving || removing}
              className="h-7 px-3 rounded border border-border bg-[var(--color-panel-hi)] hover:bg-accent text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
            >
              {testing ? "testing…" : "test"}
            </button>
            {settings.configured && (
              <button
                type="button"
                onClick={onRemove}
                disabled={removing}
                className="ml-auto h-7 px-3 rounded border border-destructive/40 text-destructive hover:bg-destructive/10 text-[12px] font-mono cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-default"
              >
                {removing ? "removing…" : "remove"}
              </button>
            )}
          </div>

          {testResult && (
            <p
              className={
                testResult.ok
                  ? "text-[11px] font-mono text-emerald-500"
                  : "text-[11px] font-mono text-destructive"
              }
            >
              {testResult.ok
                ? `ok · ${testResult.latency_ms}ms`
                : testErrorMessage(testResult.error ?? "")}
            </p>
          )}

          {settings.configured && settings.last_verified_at && (
            <p className="text-[10.5px] font-mono text-muted-foreground">
              last verified {new Date(settings.last_verified_at).toLocaleString()}
            </p>
          )}

          <div className="space-y-1 pt-2 mt-1 border-t border-border/60">
            <span className={fieldLabel}>Usage, not billing</span>
            {usageError ? (
              <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                Usage isn&apos;t available right now.
              </p>
            ) : !usage ? (
              <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                checking your usage…
              </p>
            ) : usage.totals.calls === 0 ? (
              <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                Your key hasn&apos;t been used yet.
              </p>
            ) : (
              <>
                <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                  CortexMail used your key {usage.totals.calls} times in the last{" "}
                  {usage.window_days} days.
                </p>
                {usage.totals.calls_with_total_tokens > 0 ? (
                  <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                    About {usage.totals.total_tokens.toLocaleString()} tokens total
                    {usage.totals.calls_with_total_tokens < usage.totals.calls
                      ? ` — across ${usage.totals.calls_with_total_tokens} of those calls. This provider doesn't report token counts for the rest.`
                      : "."}
                  </p>
                ) : (
                  <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                    This provider doesn&apos;t report token counts.
                  </p>
                )}
                {usage.by_stage.length > 0 && (
                  <p className="text-[10.5px] font-mono text-muted-foreground leading-snug">
                    {usage.by_stage
                      .map((s) => `${STAGE_LABELS[s.stage] ?? s.stage}: ${s.calls}`)
                      .join(" · ")}
                  </p>
                )}
              </>
            )}
            {settings.provider && USAGE_DASHBOARD_URLS[settings.provider] && (
              <a
                href={USAGE_DASHBOARD_URLS[settings.provider]}
                target="_blank"
                rel="noreferrer"
                className="block text-[10.5px] font-mono text-primary underline underline-offset-2"
              >
                See what this actually cost on {PROVIDER_LABELS[settings.provider]}
              </a>
            )}
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
