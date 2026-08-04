import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import type {
  ClassifierMixEntry,
  LlmProvider,
  LlmSettings,
  LlmTestResult,
  LlmUsage,
} from "@/lib/types";

// Mirrors the real api module's signatures for the six LLM endpoints this
// hook touches, so tests can inject fakes instead of hitting the network.
export interface LlmPanelDeps {
  getLlmSettings: () => Promise<LlmSettings>;
  putLlmSettings: (input: {
    provider: LlmProvider;
    // Optional: omitted on an update means "keep the saved key", so a user
    // who no longer has the raw string can still change other settings.
    api_key?: string;
    model: string;
    base_url?: string;
    classification_byok?: boolean;
  }) => Promise<LlmSettings>;
  testLlmSettings: () => Promise<LlmTestResult>;
  deleteLlmSettings: () => Promise<void>;
  getLlmUsage: (days: number) => Promise<LlmUsage>;
  getClassifierMix: () => Promise<{ classifier_mix: ClassifierMixEntry[] }>;
  onSessionExpired: () => void;
  toastSuccess: (message: string) => void;
  toastError: (message: string) => void;
}

interface UseLlmPanelOptions {
  // null while signed out. Every piece of state below belongs to ONE
  // account, so a change here clears it all -- see the effect at the bottom.
  userId: string | null;
  deps: LlmPanelDeps;
}

export function useLlmPanel({ userId, deps }: UseLlmPanelOptions) {
  // BYOK LLM credential -- fetched once per login alongside connections, no
  // polling; refreshed after every save/test/delete.
  const [llmSettings, setLlmSettings] = useState<LlmSettings | null>(null);
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);
  const [llmSaving, setLlmSaving] = useState(false);
  const [llmTesting, setLlmTesting] = useState(false);
  const [llmRemoving, setLlmRemoving] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState<LlmTestResult | null>(null);
  // Usage gets its own state, separate from llmSettings above -- it's fetched
  // fresh every time the modal opens (never gated on "is this null yet") since
  // background usage keeps moving even when the stored credential hasn't.
  const [llmUsage, setLlmUsage] = useState<LlmUsage | null>(null);
  const [llmUsageError, setLlmUsageError] = useState(false);
  // Classifier mix (plan §7): which classifier labeled the mail this user
  // currently has. Same tri-state contract as usage above -- null while in
  // flight or not yet fetched, a real payload once one lands, a separate
  // error flag on failure so an outage here never makes the rest of the
  // settings modal look broken.
  const [classifierMix, setClassifierMix] = useState<ClassifierMixEntry[] | null>(null);
  const [classifierMixError, setClassifierMixError] = useState(false);
  // The usage card's own dialog + range selector -- separate from the
  // settings modal above, since it opens independently (palette, or the
  // settings modal's "view usage" button).
  const [llmUsageOpen, setLlmUsageOpen] = useState(false);
  const [llmUsageDays, setLlmUsageDays] = useState(30);

  // Bumped on every save or remove of the LLM credential completes (success
  // or failure -- either way the stored credential may have changed). A test
  // in flight across that boundary captures the generation before awaiting and
  // discards its response if this has moved on, so a stale result never
  // renders against a credential it didn't actually test.
  const llmCredentialGenRef = useRef(0);
  // The stored settings themselves get a counter too, and it is bumped ONLY on
  // an account change -- never by a save or remove. Those two write settings
  // deliberately, so sharing the credential counter would make each save
  // discard its own response. What this guards is narrower: a getLlmSettings()
  // already in flight for the previous account resolving after the switch and
  // repopulating the panel with that account's credential (openLlmSettings
  // only refetches `if (!llmSettings)`, so it would then stick).
  const llmSettingsGenRef = useRef(0);
  // Usage fetches get their OWN counter. They share the credential's
  // invalidation reasons (a save or remove bumps both), but they also have
  // one of their own -- switching the card's 7/30/90 range -- and that must
  // not touch the credential generation. Sharing one ref meant a range click
  // during an in-flight Test discarded the test's response: no result, no
  // error, just the button settling silently.
  const llmUsageGenRef = useRef(0);
  // The classifier-mix fetch gets its OWN counter, separate from BOTH of the
  // above -- reusing llmUsageGenRef here would repeat exactly the bug that
  // ref's own comment describes (a usage range click silently voiding an
  // in-flight credential test), just with the mix fetch as the new victim.
  // A credential save/remove genuinely invalidates an in-flight mix fetch
  // (the thing being summarized just changed), so those two bump this ref
  // too -- but nothing else does.
  const llmMixGenRef = useRef(0);

  useEffect(() => {
    // Every piece of LLM state below belongs to ONE account, so none of it may
    // survive a switch to another. Logging out is setToken(null)+setUser(null)
    // with no reload and Console never remounts, so without this the next
    // person to sign in on this tab inherits it all.
    //
    // llmSettings is the one that actually bites: openLlmSettings only fetches
    // `if (!llmSettings)`, so a stale value isn't a brief flash before the real
    // one lands -- it's what the panel shows until a save, remove, or test
    // happens to refresh it. That panel renders `key_suffix`, the last four
    // characters of the previous account's API key.
    //
    // Bumping each generation ref discards any fetch already in flight for the
    // old account, which would otherwise land after this clear and put the same
    // data straight back.
    llmCredentialGenRef.current++;
    llmSettingsGenRef.current++;
    llmUsageGenRef.current++;
    llmMixGenRef.current++;
    setLlmSettings(null);
    setLlmUsage(null);
    setLlmUsageError(false);
    setLlmTestResult(null);
    setClassifierMix(null);
    setClassifierMixError(false);
    // Close both panels too. They're plain booleans that nothing else resets,
    // so without this the next person to sign in lands with an AI panel open
    // that they never opened.
    setLlmSettingsOpen(false);
    setLlmUsageOpen(false);
    // NOTE ON TIMING: this is a post-commit effect, so it clears AFTER the
    // first render for a new user. That's only safe because every path that
    // installs a user runs while the previous one is already null -- signing
    // out sets null first, and the login screens only ever fire from there.
    // So this has already run once on the way down to null. If an in-app
    // account SWITCH is ever added (A straight to B, no null in between),
    // that assumption dies and this must move to a synchronous reset before
    // setUser, or B's first frame renders A's credential.
  }, [userId]);

  const refreshLlmSettings = useCallback(async () => {
    const generation = llmSettingsGenRef.current;
    try {
      const next = await deps.getLlmSettings();
      if (generation !== llmSettingsGenRef.current) return; // account changed -- discard
      setLlmSettings(next);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) deps.onSessionExpired();
    }
  }, [deps]);

  // Usage changes constantly (every ingested message can add to it), so
  // unlike settings above it's never cached against a "do we already have
  // it?" check -- always re-fetched. Guards on llmUsageGenRef, which a range
  // change bumps on its own and which save/remove bump alongside the
  // credential's -- so an out-of-order response (a 7d answered after a
  // superseded 30d request) never clobbers the range on screen, without a
  // range click reaching into an unrelated in-flight credential test.
  const refreshLlmUsage = useCallback(
    async (days: number) => {
      const generation = llmUsageGenRef.current;
      try {
        const usage = await deps.getLlmUsage(days);
        if (generation !== llmUsageGenRef.current) return; // superseded mid-flight -- discard
        setLlmUsage(usage);
        setLlmUsageError(false);
      } catch (e) {
        if (generation !== llmUsageGenRef.current) return; // ditto for a stale failure
        if (e instanceof ApiError && e.status === 401) {
          deps.onSessionExpired();
          return;
        }
        // A usage outage must never make the rest of this modal look broken --
        // no toast, just a quiet fallback the panel itself renders.
        setLlmUsage(null);
        setLlmUsageError(true);
      }
    },
    [deps],
  );

  // Which classifier labeled the mail this user currently has -- point-in-
  // time state (plan §7), so unlike usage above there's no "range" to pass
  // through; always re-fetched on open since it can change any time new mail
  // is ingested and classified. Guards on llmMixGenRef, its own counter --
  // see that ref's comment for why it can't share llmUsageGenRef.
  const refreshClassifierMix = useCallback(async () => {
    // Claim a new generation up front rather than reading the current one:
    // two refreshes can overlap (open, close, reopen), and without this both
    // read the same value, so an older response landing second overwrites a
    // newer one. Bumping here makes the most recent caller the only winner.
    const generation = ++llmMixGenRef.current;
    try {
      const mix = await deps.getClassifierMix();
      if (generation !== llmMixGenRef.current) return; // superseded mid-flight -- discard
      setClassifierMix(mix.classifier_mix);
      setClassifierMixError(false);
    } catch (e) {
      if (generation !== llmMixGenRef.current) return; // ditto for a stale failure
      if (e instanceof ApiError && e.status === 401) {
        deps.onSessionExpired();
        return;
      }
      // Same rule as usage's own outage handling -- a failure here must never
      // make the rest of the settings modal look broken.
      setClassifierMix(null);
      setClassifierMixError(true);
    }
  }, [deps]);

  // Opening always starts the modal from a clean test result -- otherwise a
  // stale ok/error from a previous visit would flash before the user does
  // anything this time. If the post-login fetch never landed (settings is
  // still null), retry it here and await it -- otherwise every entry point
  // into this modal (palette, accounts menu, agenda CTA) would silently do
  // nothing on a bad connection. Usage, unlike settings, is fetched every
  // single time regardless of what's already loaded -- it's never stale-safe
  // to skip.
  const openLlmSettings = useCallback(async () => {
    setLlmTestResult(null);
    void refreshLlmUsage(llmUsageDays);
    void refreshClassifierMix();
    if (!llmSettings) {
      const generation = llmSettingsGenRef.current;
      try {
        const next = await deps.getLlmSettings();
        if (generation !== llmSettingsGenRef.current) return; // account changed -- discard
        setLlmSettings(next);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          deps.onSessionExpired();
        } else {
          deps.toastError((e as Error).message || "could not load AI settings");
        }
        return;
      }
    }
    setLlmSettingsOpen(true);
  }, [llmSettings, deps, refreshLlmUsage, llmUsageDays, refreshClassifierMix]);

  // Opens the usage card directly (palette, or the settings modal's "view
  // usage" button) -- same "always refetch, never stale-safe to skip" rule
  // as the settings modal's own usage fetch above.
  const openLlmUsage = useCallback(() => {
    setLlmUsageOpen(true);
    void refreshLlmUsage(llmUsageDays);
  }, [refreshLlmUsage, llmUsageDays]);

  // Changing the range invalidates whatever usage fetch is still in flight
  // for the old window, so an out-of-order response (a 7d answered after a
  // since-superseded 30d request) never clobbers the range on screen. Bumps
  // ONLY the usage generation -- the credential hasn't changed, and touching
  // its counter here would make a range click silently void an in-flight
  // credential test.
  const changeLlmUsageDays = useCallback(
    (days: number) => {
      setLlmUsageDays(days);
      llmUsageGenRef.current++;
      void refreshLlmUsage(days);
    },
    [refreshLlmUsage],
  );

  const doSaveLlmSettings = useCallback(
    async (input: {
      provider: LlmProvider;
      // Optional: omitted on an update means "keep the saved key", so a user
      // who no longer has the raw string can still change other settings.
      api_key?: string;
      model: string;
      base_url?: string;
      classification_byok?: boolean;
    }) => {
      setLlmSaving(true);
      let saved = false;
      // Guarded on the settings counter, not the credential one: a save is
      // meant to write settings, so it must not invalidate itself. This only
      // discards the response if the ACCOUNT changed while the PUT was in
      // flight, in which case it belongs to someone else's panel.
      const generation = llmSettingsGenRef.current;
      try {
        const next = await deps.putLlmSettings(input);
        if (generation !== llmSettingsGenRef.current) return; // account changed -- discard
        setLlmSettings(next);
        setLlmTestResult(null);
        deps.toastSuccess("AI settings saved");
        saved = true;
      } catch (e) {
        deps.toastError((e as Error).message || "could not save these settings");
      } finally {
        // The credential may have changed either way -- bump all three so
        // any test, usage fetch, AND mix fetch that started before this save
        // discards its response when it lands. A credential change genuinely
        // invalidates an in-flight mix fetch (the thing it summarizes just
        // changed), so it's grouped with the credential/usage bumps here --
        // never with a usage-range-only change.
        llmCredentialGenRef.current++;
        llmUsageGenRef.current++;
        llmMixGenRef.current++;
        setLlmSaving(false);
      }
      // Refetch only on success, and only after the bump above -- otherwise
      // this fetch would capture the pre-bump generation and immediately
      // discard its own result once the finally block ran.
      if (saved) {
        void refreshLlmUsage(llmUsageDays);
        void refreshClassifierMix();
      }
    },
    [deps, refreshLlmUsage, llmUsageDays, refreshClassifierMix],
  );

  const doTestLlmSettings = useCallback(async () => {
    const generation = llmCredentialGenRef.current;
    setLlmTesting(true);
    setLlmTestResult(null);
    try {
      const res = await deps.testLlmSettings();
      if (generation !== llmCredentialGenRef.current) return; // credential changed mid-flight -- discard
      setLlmTestResult(res);
      // A successful test stamps last_verified_at server-side -- pull that
      // in so the modal reflects it without a second explicit refresh.
      if (res.ok) refreshLlmSettings();
    } catch (e) {
      if (generation !== llmCredentialGenRef.current) return; // ditto for a stale failure
      deps.toastError((e as Error).message || "could not test this credential");
    } finally {
      setLlmTesting(false);
    }
  }, [deps, refreshLlmSettings]);

  const doRemoveLlmSettings = useCallback(async () => {
    setLlmRemoving(true);
    let removed = false;
    try {
      await deps.deleteLlmSettings();
      setLlmTestResult(null);
      await refreshLlmSettings();
      deps.toastSuccess("AI credential removed");
      removed = true;
    } catch (e) {
      deps.toastError((e as Error).message || "could not remove this credential");
    } finally {
      // Same as the save path: a removal invalidates an in-flight test, an
      // in-flight usage fetch, and an in-flight mix fetch alike.
      llmCredentialGenRef.current++;
      llmUsageGenRef.current++;
      llmMixGenRef.current++;
      setLlmRemoving(false);
    }
    // Same ordering rule as doSaveLlmSettings: refetch after the generation
    // bump above, and only once the remove actually went through.
    if (removed) {
      void refreshLlmUsage(llmUsageDays);
      void refreshClassifierMix();
    }
  }, [deps, refreshLlmSettings, refreshLlmUsage, llmUsageDays, refreshClassifierMix]);

  return {
    llmSettings,
    llmSettingsOpen,
    setLlmSettingsOpen,
    llmSaving,
    llmTesting,
    llmRemoving,
    llmTestResult,
    llmUsage,
    llmUsageError,
    classifierMix,
    classifierMixError,
    llmUsageOpen,
    setLlmUsageOpen,
    llmUsageDays,
    refreshLlmSettings,
    refreshLlmUsage,
    refreshClassifierMix,
    openLlmSettings,
    openLlmUsage,
    changeLlmUsageDays,
    doSaveLlmSettings,
    doTestLlmSettings,
    doRemoveLlmSettings,
  };
}
