import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import type {
  ClassifierMixEntry,
  LlmCredentialSummary,
  LlmProvider,
  LlmSettings,
  LlmTestResult,
  LlmUsage,
  SettingsTab,
} from "@/lib/types";

// Mirrors the real api module's signatures for the ten LLM endpoints this
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
    classification_fallback_local?: boolean;
  }) => Promise<LlmSettings>;
  testLlmSettings: () => Promise<LlmTestResult>;
  deleteLlmSettings: () => Promise<void>;
  getLlmUsage: (days: number) => Promise<LlmUsage>;
  getClassifierMix: () => Promise<{ classifier_mix: ClassifierMixEntry[] }>;
  // Multi-credential surface (2026-08-19-multi-credential-llm-profiles plan)
  // -- listLlmCredentials/createLlmCredential/activateLlmCredential/
  // deleteLlmCredential mirror api.ts's own signatures for those four.
  listLlmCredentials: () => Promise<LlmCredentialSummary[]>;
  createLlmCredential: (input: {
    name: string;
    provider: LlmProvider;
    api_key: string;
    model: string;
    base_url?: string;
    classification_byok?: boolean;
    classification_fallback_local?: boolean;
  }) => Promise<LlmCredentialSummary>;
  activateLlmCredential: (id: string) => Promise<LlmCredentialSummary>;
  deleteLlmCredential: (id: string) => Promise<void>;
  onSessionExpired: () => void;
  toastSuccess: (message: string) => void;
  toastError: (message: string) => void;
}

interface UseLlmPanelOptions {
  // null while signed out. Every piece of state below belongs to ONE
  // account, so a change here clears it all -- see the effect at the bottom.
  userId: string | null;
  deps: LlmPanelDeps;
  // App owns the settings dialog's open/tab state (settings-card plan §3.3)
  // -- this hook only ever needs to say WHICH tab an entry point wants open,
  // never whether the dialog itself is open. `openLlmSettings`/`openLlmUsage`
  // below are thin wrappers over this.
  openSettings: (tab: SettingsTab) => void;
}

export function useLlmPanel({ userId, deps, openSettings }: UseLlmPanelOptions) {
  // BYOK LLM credential -- fetched once per login alongside connections, no
  // polling; refreshed after every save/test/delete.
  const [llmSettings, setLlmSettings] = useState<LlmSettings | null>(null);
  // True on a failed load, current-generation, never a 401 (that's a session
  // bounce, handled separately) -- lets the ai tab tell "still loading" apart
  // from "actually failed" instead of an indefinite "loading…" (plan §3.2/
  // P2-2). Cleared at the start of every fetch, on a success, and on user
  // change -- never sticks around describing a stale account's failure.
  const [llmSettingsError, setLlmSettingsError] = useState(false);
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
  const [llmUsageDays, setLlmUsageDays] = useState(30);

  // Multi-credential list (2026-08-19-multi-credential-llm-profiles plan) --
  // every credential this user owns, active or not; null while the fetch
  // hasn't landed yet, same tri-state contract as classifierMix above.
  // Re-fetched on every "ai" tab activation (App's handleSettingsTabChange),
  // not just the dialog's first open, same reasoning as usage/mix: a switch
  // made in another tab/session must show up here too.
  const [llmCredentials, setLlmCredentials] = useState<LlmCredentialSummary[] | null>(null);
  const [llmCredentialsError, setLlmCredentialsError] = useState(false);
  const [llmCreatingCredential, setLlmCreatingCredential] = useState(false);
  // Which credential's activate/delete call is in flight, if any -- lets the
  // list disable just the one row a click is already targeting, rather than
  // freezing the whole list for an unrelated in-flight action.
  const [llmActivatingCredentialId, setLlmActivatingCredentialId] = useState<string | null>(null);
  const [llmDeletingCredentialId, setLlmDeletingCredentialId] = useState<string | null>(null);
  // A by-id delete can still 409 (the target became active a moment before
  // this request landed -- activate and delete aren't optimistic-locked
  // against each other, D5) -- carries which row it was about so the list
  // can show the message on that row specifically, not a generic toast.
  const [llmCredentialDeleteError, setLlmCredentialDeleteError] = useState<{
    id: string;
    message: string;
  } | null>(null);

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
  // The credential-list fetch gets its OWN counter too, same claim-first
  // pattern as llmUsageGenRef/llmMixGenRef above -- two overlapping list
  // fetches (e.g. re-opening the ai tab quickly) must let the most recent
  // one win, not whichever happens to resolve last. Every structural
  // mutation (create/activate/delete/save/remove) ALSO bumps this directly,
  // synchronously before its own request goes out -- a list GET that was
  // already in flight when one of those starts must be dropped, not land
  // afterward and repaint the list with data the mutation just made stale.
  const llmCredentialsGenRef = useRef(0);

  useEffect(() => {
    // Every piece of LLM state below belongs to ONE account, so none of it may
    // survive a switch to another. Logging out is setToken(null)+setUser(null)
    // with no reload and Console never remounts, so without this the next
    // person to sign in on this tab inherits it all.
    //
    // llmSettings is the one that actually bites: it's what the panel shows
    // until the next login's own refreshLlmSettings() call lands, not just a
    // brief flash before the real one arrives. That panel renders
    // `key_suffix`, the last four characters of the previous account's API
    // key.
    //
    // Bumping each generation ref discards any fetch already in flight for the
    // old account, which would otherwise land after this clear and put the same
    // data straight back.
    llmCredentialGenRef.current++;
    llmSettingsGenRef.current++;
    llmUsageGenRef.current++;
    llmMixGenRef.current++;
    llmCredentialsGenRef.current++;
    setLlmSettings(null);
    setLlmSettingsError(false);
    setLlmUsage(null);
    setLlmUsageError(false);
    setLlmTestResult(null);
    setClassifierMix(null);
    setClassifierMixError(false);
    setLlmCredentials(null);
    setLlmCredentialsError(false);
    setLlmCredentialDeleteError(null);
    // The three structural-action flags below are per-id/per-boolean, not
    // per-account data, but they're exactly as account-scoped as everything
    // above: without this, the next person to sign in on this tab can
    // inherit "creating"/"activating X"/"deleting Y" from whoever was just
    // signed in, which freezes their own controls disabled for an action
    // that isn't theirs and was never actually in flight for them.
    setLlmCreatingCredential(false);
    setLlmActivatingCredentialId(null);
    setLlmDeletingCredentialId(null);
    // The three singular-flow flags get the same treatment for the same
    // reason -- and because their finally blocks are generation-guarded,
    // this reset is also what un-sticks them after an account switch (a
    // stale completion deliberately no longer touches them).
    setLlmSaving(false);
    setLlmTesting(false);
    setLlmRemoving(false);
    // The settings dialog itself closes via App's own account-scoped reset
    // (settingsPanel -> null alongside its other resets) -- this hook no
    // longer owns that state, just the data it's built from.
    // NOTE ON TIMING: this is a post-commit effect, so it clears AFTER the
    // first render for a new user. That's only safe because every path that
    // installs a user runs while the previous one is already null -- signing
    // out sets null first, and the login screens only ever fire from there.
    // So this has already run once on the way down to null. If an in-app
    // account SWITCH is ever added (A straight to B, no null in between),
    // that assumption dies and this must move to a synchronous reset before
    // setUser, or B's first frame renders A's credential.
  }, [userId]);

  // Shared by the silent login-time refresh and the ai tab's user-initiated
  // retry -- same fetch, same generation guard, same llmSettingsError
  // bookkeeping. The only difference is whether a failure gets a toast
  // (plan §3.2: the silent refresh must never surface an unsolicited one).
  const fetchLlmSettings = useCallback(
    async (opts: { toastOnError: boolean }) => {
      const generation = llmSettingsGenRef.current;
      setLlmSettingsError(false);
      try {
        const next = await deps.getLlmSettings();
        if (generation !== llmSettingsGenRef.current) return; // account changed -- discard
        setLlmSettings(next);
        setLlmSettingsError(false);
      } catch (e) {
        if (generation !== llmSettingsGenRef.current) return; // stale failure -- discard
        if (e instanceof ApiError && e.status === 401) {
          deps.onSessionExpired();
          return;
        }
        setLlmSettingsError(true);
        if (opts.toastOnError) {
          deps.toastError((e as Error).message || "could not load AI settings");
        }
      }
    },
    [deps],
  );

  const refreshLlmSettings = useCallback(
    () => fetchLlmSettings({ toastOnError: false }),
    [fetchLlmSettings],
  );

  // The ai tab's retry button (plan §3.2/P2-1) -- user-initiated, so unlike
  // the silent refresh above, a failure here does get a toast.
  const retryLlmSettings = useCallback(
    () => fetchLlmSettings({ toastOnError: true }),
    [fetchLlmSettings],
  );

  // Usage changes constantly (every ingested message can add to it), so
  // unlike settings above it's never cached against a "do we already have
  // it?" check -- always re-fetched. Guards on llmUsageGenRef, which a range
  // change bumps on its own and which save/remove bump alongside the
  // credential's -- so an out-of-order response (a 7d answered after a
  // superseded 30d request) never clobbers the range on screen, without a
  // range click reaching into an unrelated in-flight credential test.
  const refreshLlmUsage = useCallback(
    async (days: number) => {
      // Claim a new generation up front rather than reading the current one:
      // openLlmSettings and openLlmUsage (or the post-save refetch) can start
      // overlapping usage fetches, and without this both would read the same
      // value, so an older response landing second overwrites a newer one.
      // Bumping here makes the most recent caller the only winner.
      const generation = ++llmUsageGenRef.current;
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

  // Every credential this user owns (2026-08-19-multi-credential-llm-
  // profiles plan) -- always re-fetched, same claim-first generation guard
  // as refreshLlmUsage/refreshClassifierMix above (see llmCredentialsGenRef's
  // own comment).
  const refreshLlmCredentials = useCallback(async () => {
    const generation = ++llmCredentialsGenRef.current;
    try {
      const items = await deps.listLlmCredentials();
      if (generation !== llmCredentialsGenRef.current) return; // superseded mid-flight -- discard
      setLlmCredentials(items);
      setLlmCredentialsError(false);
    } catch (e) {
      if (generation !== llmCredentialsGenRef.current) return; // ditto for a stale failure
      if (e instanceof ApiError && e.status === 401) {
        deps.onSessionExpired();
        return;
      }
      // Same rule as usage/mix's own outage handling -- a failure here must
      // never make the rest of the settings modal look broken. The single-
      // credential form still works off llmSettings regardless.
      setLlmCredentials(null);
      setLlmCredentialsError(true);
    }
  }, [deps]);

  // Thin wrappers over App's openSettings (settings-card plan §3.3) -- the
  // dialog itself now owns open/close and every tab stays mounted, so the ai
  // tab renders its own loading/error/retry state off llmSettings/
  // llmSettingsError instead of this hook re-fetching on open. The usage/mix
  // prefetch-on-activation happens inside openSettings itself (App wires it
  // to fire on the SAME tab switch this triggers, per plan §3.3's "must keep
  // firing on tab switch, not just dialog open"), not duplicated here.
  //
  // Opening always starts from a clean test result -- otherwise a stale ok/
  // error from a previous visit would flash before the user does anything
  // this time.
  const openLlmSettings = useCallback(() => {
    setLlmTestResult(null);
    openSettings("ai");
  }, [openSettings]);

  // Opens the usage tab directly (palette, or the ai tab's "view usage"
  // button, which now just switches tabs -- see SettingsDialog).
  const openLlmUsage = useCallback(() => {
    openSettings("usage");
  }, [openSettings]);

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
      classification_fallback_local?: boolean;
    }) => {
      setLlmSaving(true);
      let saved = false;
      // Bumped synchronously, before the PUT even goes out -- a save can
      // change provider/model/key_suffix on the active row, so a credential
      // list GET already in flight when this starts must be dropped rather
      // than allowed to land afterward and repaint the list with what the
      // row looked like before this save.
      llmCredentialsGenRef.current++;
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
        // never with a usage-range-only change. Identity-guarded like the
        // create/activate paths: a stale save finishing after an account
        // switch must not invalidate the new account's in-flight fetches.
        if (generation === llmSettingsGenRef.current) {
          llmCredentialGenRef.current++;
          llmUsageGenRef.current++;
          llmMixGenRef.current++;
          // Inside the identity guard like the bumps -- the identity-reset
          // effect owns clearing this flag for the next account.
          setLlmSaving(false);
        }
      }
      // Refetch only on success, and only after the bump above -- otherwise
      // this fetch would capture the pre-bump generation and immediately
      // discard its own result once the finally block ran.
      if (saved) {
        void refreshLlmUsage(llmUsageDays);
        void refreshClassifierMix();
        // The row this save touched can have a different provider/model/
        // key_suffix now -- the list has to catch up too, not just the
        // singular view. refreshLlmCredentials claims its own fresh
        // generation, so it isn't dropped by the bump above.
        void refreshLlmCredentials();
      }
    },
    [deps, refreshLlmUsage, llmUsageDays, refreshClassifierMix, refreshLlmCredentials],
  );

  const doTestLlmSettings = useCallback(async () => {
    const generation = llmCredentialGenRef.current;
    // Identity captured separately from the credential counter above: the
    // counter also bumps on SAME-identity mutations (a save finishing
    // mid-test), which should still release the flag below -- only an
    // account switch hands the flag over to the identity-reset effect.
    const identityGeneration = llmSettingsGenRef.current;
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
      // Identity-guarded only -- a stale test from a signed-out account
      // must not clear the flag out from under whoever owns the panel now
      // (the identity-reset effect did that for them), but a same-identity
      // credential change mid-test still releases it normally.
      if (identityGeneration === llmSettingsGenRef.current) setLlmTesting(false);
    }
  }, [deps, refreshLlmSettings]);

  const doRemoveLlmSettings = useCallback(async () => {
    setLlmRemoving(true);
    let removed = false;
    // Captured for the finally block's identity guard, same reasoning as
    // doSaveLlmSettings: only discard on an ACCOUNT change mid-flight.
    const generation = llmSettingsGenRef.current;
    // Bumped synchronously, before the DELETE goes out -- this is the kill
    // switch (D4), and a credential list GET that was already in flight
    // when it started must be dropped, not land afterward and resurrect
    // the rows this is about to wipe over the clear below.
    llmCredentialsGenRef.current++;
    try {
      // Kill switch (D4): wipes EVERY credential the caller owns, not just
      // the active one -- so the credential list has to clear right along
      // with the singular settings view.
      await deps.deleteLlmSettings();
      // Identity first: a stale removal resolving after an account switch
      // belongs to whoever was signed in before -- it must not clear the
      // new account's list, refresh their settings, or toast at them. Only
      // then re-claim the list generation, now that the DELETE has actually
      // committed: a refresh that started while it was in flight (say, the
      // ai tab getting reopened) can hold a NEWER number off the bump above
      // while still reading PRE-delete data, and without the re-claim that
      // stale-but-newer-numbered refresh would land after this clear and
      // resurrect the very rows this just wiped. (Synchronously after the
      // increment the claim check is trivially true -- it's kept for shape
      // parity with create/activate/delete, where awaits separate the two.)
      if (generation !== llmSettingsGenRef.current) return;
      setLlmTestResult(null);
      const postCredentialsGeneration = ++llmCredentialsGenRef.current;
      if (postCredentialsGeneration === llmCredentialsGenRef.current) {
        setLlmCredentials([]);
        setLlmCredentialsError(false);
        setLlmCredentialDeleteError(null);
      }
      await refreshLlmSettings();
      // Second identity check: the refresh above is its own await, and the
      // account can change during it too -- the toast and the usage/mix
      // follow-ups (gated on `removed` below) belong to whoever started
      // this removal, never to whoever is signed in now.
      if (generation !== llmSettingsGenRef.current) return;
      deps.toastSuccess("AI credential removed");
      removed = true;
    } catch (e) {
      if (generation !== llmSettingsGenRef.current) return;
      deps.toastError((e as Error).message || "could not remove this credential");
    } finally {
      // Same as the save path: a removal invalidates an in-flight test, an
      // in-flight usage fetch, and an in-flight mix fetch alike -- and the
      // same identity guard applies, so a stale removal can't invalidate
      // the next account's fetches.
      if (generation === llmSettingsGenRef.current) {
        llmCredentialGenRef.current++;
        llmUsageGenRef.current++;
        llmMixGenRef.current++;
        // Inside the identity guard like the bumps: a stale completion
        // must not touch the new account's flag -- the identity-reset
        // effect is what cleared it for them.
        setLlmRemoving(false);
      }
    }
    // Same ordering rule as doSaveLlmSettings: refetch after the generation
    // bump above, and only once the remove actually went through.
    if (removed) {
      void refreshLlmUsage(llmUsageDays);
      void refreshClassifierMix();
    }
  }, [deps, refreshLlmSettings, refreshLlmUsage, llmUsageDays, refreshClassifierMix]);

  // Adds a new named credential. Refreshes BOTH the credential list and the
  // singular settings object, since a first credential is created active
  // (D5) -- that changes what GET /settings/llm reports too, not just what
  // shows up in the list. Fetched together (Promise.all) and applied in one
  // state update so the two never land a render apart, which is what lets
  // LlmSettingsSection re-key its hydration cleanly off the active id.
  const doCreateLlmCredential = useCallback(
    async (input: {
      name: string;
      provider: LlmProvider;
      api_key: string;
      model: string;
      base_url?: string;
      classification_byok?: boolean;
      classification_fallback_local?: boolean;
    }) => {
      setLlmCreatingCredential(true);
      // Guarded on the settings counter, same convention doSaveLlmSettings
      // uses: this only discards its own result if the ACCOUNT changed
      // while the request was in flight. Captured up front so a completion
      // landing after a switch can tell it's no longer the current identity
      // -- the account-change effect already reset creatingCredential for
      // whoever's here now, and they may have started their own create by
      // the time this settles, so a stale completion must never touch their
      // state or toast at them about an action they didn't take.
      const generation = llmSettingsGenRef.current;
      // Bumped synchronously, before the POST goes out -- a new credential
      // changes the list (and, if it's the first one, the active row too),
      // so a list GET already in flight must be dropped rather than allowed
      // to land afterward without the row this just added. Only the side
      // effect matters here -- the reconciliation below re-claims its own
      // generation once the POST actually commits.
      llmCredentialsGenRef.current++;
      try {
        await deps.createLlmCredential(input);
        // The POST has committed on the server by this point, so re-claim
        // the generation here instead of trusting the number captured
        // before it went out. A refresh that started mid-flight (re-
        // opening the ai tab, say) can claim a NEWER number off the bump
        // above while it's still reading PRE-commit data -- without re-
        // claiming here, this reconciliation, the one with the actually
        // fresh data, would lose that race and get thrown away.
        const postCredentialsGeneration = ++llmCredentialsGenRef.current;
        const [items, settings] = await Promise.all([
          deps.listLlmCredentials(),
          deps.getLlmSettings(),
        ]);
        if (
          generation === llmSettingsGenRef.current &&
          postCredentialsGeneration === llmCredentialsGenRef.current
        ) {
          setLlmCredentials(items);
          setLlmCredentialsError(false);
          setLlmSettings(settings);
          setLlmTestResult(null);
          deps.toastSuccess(`Added "${input.name}"`);
        }
      } catch (e) {
        if (generation === llmSettingsGenRef.current) {
          deps.toastError((e as Error).message || "could not add this credential");
        }
      } finally {
        // Guarded the same as the UI writes above -- a completion landing
        // after an account switch belongs to whoever was signed in before,
        // and must not bump generations the new identity's own in-flight
        // test/usage/mix fetches are relying on to still be current.
        if (generation === llmSettingsGenRef.current) {
          llmCredentialGenRef.current++;
          llmUsageGenRef.current++;
          llmMixGenRef.current++;
          setLlmCreatingCredential(false);
        }
      }
    },
    [deps],
  );

  // Switches the caller's active credential. Same Promise.all/single-update
  // pattern as doCreateLlmCredential above, for the same reason: the list
  // and the singular settings view both change together, and applying them
  // apart would let the form hydrate against a mismatched pair for a frame.
  const doActivateLlmCredential = useCallback(
    async (id: string) => {
      setLlmActivatingCredentialId(id);
      // Same identity guard as doCreateLlmCredential above -- captured up
      // front so a completion landing after a switch never clears a
      // different user's own in-flight activate or toasts at them.
      const generation = llmSettingsGenRef.current;
      // Bumped synchronously, before the PATCH goes out -- switching which
      // credential is active changes which row is marked active in the
      // list, so a list GET already in flight must be dropped. Only the
      // side effect matters here -- see the re-claim below.
      llmCredentialsGenRef.current++;
      try {
        await deps.activateLlmCredential(id);
        // Same re-claim as doCreateLlmCredential above, and for the same
        // reason: the PATCH has committed by this point, so this
        // reconciliation needs to win over a refresh that read stale data
        // while it was still in flight, not lose to it.
        const postCredentialsGeneration = ++llmCredentialsGenRef.current;
        const [items, settings] = await Promise.all([
          deps.listLlmCredentials(),
          deps.getLlmSettings(),
        ]);
        if (
          generation === llmSettingsGenRef.current &&
          postCredentialsGeneration === llmCredentialsGenRef.current
        ) {
          setLlmCredentials(items);
          setLlmCredentialsError(false);
          setLlmSettings(settings);
          setLlmTestResult(null);
          setLlmCredentialDeleteError(null);
        }
      } catch (e) {
        if (generation === llmSettingsGenRef.current) {
          deps.toastError((e as Error).message || "could not switch credentials");
        }
      } finally {
        // Guarded the same as the UI writes above -- see doCreateLlmCredential's
        // finally for why.
        if (generation === llmSettingsGenRef.current) {
          llmCredentialGenRef.current++;
          llmUsageGenRef.current++;
          llmMixGenRef.current++;
          setLlmActivatingCredentialId(null);
        }
      }
    },
    [deps],
  );

  // Removes one inactive credential. Never touches llmSettings -- deleting a
  // credential that ISN'T active can't change what GET /settings/llm
  // reports, so only the list needs a refetch.
  const doDeleteLlmCredential = useCallback(
    async (id: string) => {
      setLlmDeletingCredentialId(id);
      setLlmCredentialDeleteError(null);
      // Same identity guard as the other two structural actions above.
      const generation = llmSettingsGenRef.current;
      // Bumped synchronously, before the DELETE goes out -- same reasoning
      // as create/activate above: the row this removes has to stay gone,
      // not get reintroduced by a list GET that started before this call.
      // Only the side effect matters here -- see the re-claim below.
      llmCredentialsGenRef.current++;
      try {
        await deps.deleteLlmCredential(id);
        // Same re-claim as create/activate above -- the DELETE has
        // committed by this point, so this reconciliation needs to win
        // over a refresh that read stale (pre-delete) data while it was
        // still in flight.
        const postCredentialsGeneration = ++llmCredentialsGenRef.current;
        const items = await deps.listLlmCredentials();
        if (
          generation === llmSettingsGenRef.current &&
          postCredentialsGeneration === llmCredentialsGenRef.current
        ) {
          setLlmCredentials(items);
          setLlmCredentialsError(false);
          deps.toastSuccess("Credential removed");
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          // Blocked because this row is the active credential -- the delete
          // button is hidden for the active row, so reaching this means it
          // was activated elsewhere a moment before this request landed.
          // Surfaced on the row itself, not a toast, since the row is what
          // the message is about.
          if (generation === llmSettingsGenRef.current) {
            setLlmCredentialDeleteError({
              id,
              message: "This is your active credential — switch to another one first.",
            });
          }
        } else if (generation === llmSettingsGenRef.current) {
          deps.toastError((e as Error).message || "could not remove this credential");
        }
      } finally {
        if (generation === llmSettingsGenRef.current) {
          setLlmDeletingCredentialId(null);
        }
      }
    },
    [deps],
  );

  return {
    llmSettings,
    llmSettingsError,
    llmSaving,
    llmTesting,
    llmRemoving,
    llmTestResult,
    llmUsage,
    llmUsageError,
    classifierMix,
    classifierMixError,
    llmUsageDays,
    llmCredentials,
    llmCredentialsError,
    llmCreatingCredential,
    llmActivatingCredentialId,
    llmDeletingCredentialId,
    llmCredentialDeleteError,
    refreshLlmSettings,
    retryLlmSettings,
    refreshLlmUsage,
    refreshClassifierMix,
    refreshLlmCredentials,
    openLlmSettings,
    openLlmUsage,
    changeLlmUsageDays,
    doSaveLlmSettings,
    doTestLlmSettings,
    doRemoveLlmSettings,
    doCreateLlmCredential,
    doActivateLlmCredential,
    doDeleteLlmCredential,
  };
}
