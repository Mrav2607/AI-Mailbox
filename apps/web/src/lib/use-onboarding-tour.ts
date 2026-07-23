import { useCallback, useEffect, useRef, useState } from "react";

import type { Panels } from "@/App";
import { TOUR_VERSION } from "@/lib/layout";
import type { BucketKey } from "@/lib/types";
import {
  resolveTourTarget,
  tourLockedPopover,
  TOUR_STEPS,
  type TourPrecondition,
  type TourTargetResolution,
} from "@/lib/tour-steps";

type PanelKey = keyof Panels;

export interface TourDeps {
  showPanel: (panel: PanelKey) => void;
  setBucket: (slug: BucketKey) => void;
  setIngestOpen: (open: boolean) => void;
  setAccountsOpen: (open: boolean) => void;
  snapshotPanels: () => Panels;
  restorePanels: (snapshot: Panels) => void;
}

interface AutoStartGate {
  authChecked: boolean;
  hasUser: boolean;
  tourVersion: number;
  firstListLoadSettled: boolean;
  narrowViewport: boolean;
}

interface UseOnboardingTourOptions extends AutoStartGate {
  deps: TourDeps;
  setTourVersion: (version: number) => void;
}

export function shouldAutoStartTour(gate: AutoStartGate): boolean {
  return (
    gate.authChecked &&
    gate.hasUser &&
    gate.tourVersion < TOUR_VERSION &&
    gate.firstListLoadSettled &&
    !gate.narrowViewport
  );
}

export function enforceTourPreconditions(
  preconditions: TourPrecondition[],
  deps: TourDeps,
): void {
  // Tour-managed popovers are declarative per step: close whatever this step
  // doesn't ask for before opening what it does, so leaving a step (in either
  // direction) never strands its popover on screen.
  if (!preconditions.includes("open-ingest")) deps.setIngestOpen(false);
  if (!preconditions.includes("open-accounts")) deps.setAccountsOpen(false);
  for (const precondition of preconditions) {
    switch (precondition) {
      case "show-sidebar":
        deps.showPanel("sidebar");
        break;
      case "select-needs-reply":
        deps.setBucket("needs_reply");
        break;
      case "show-detail":
        deps.showPanel("detail");
        break;
      case "show-prediction":
        deps.showPanel("prediction");
        break;
      case "open-ingest":
        deps.setIngestOpen(true);
        break;
      case "open-accounts":
        deps.setAccountsOpen(true);
        break;
    }
  }
}

export function useOnboardingTour({
  authChecked,
  hasUser,
  tourVersion,
  firstListLoadSettled,
  narrowViewport,
  deps,
  setTourVersion,
}: UseOnboardingTourOptions) {
  const [tourActive, setTourActive] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const [targetResolution, setTargetResolution] =
    useState<TourTargetResolution | null>(null);
  const autoStartLatched = useRef(false);
  const panelSnapshot = useRef<Panels | null>(null);
  const direction = useRef<1 | -1>(1);

  const restorePanelSnapshot = useCallback(() => {
    if (!panelSnapshot.current) return;
    deps.restorePanels(panelSnapshot.current);
    panelSnapshot.current = null;
  }, [deps]);

  // finish keeps the ingest panel open as the v1 CTA; skip (and any other
  // terminal path) closes it — declining shouldn't leave a form in the
  // user's face. Either way the accounts popover always closes: it's a
  // transient disclosure with no "leave it open" story.
  const completeTour = useCallback(
    (opts: { keepIngestOpen: boolean }) => {
      restorePanelSnapshot();
      deps.setAccountsOpen(false);
      if (!opts.keepIngestOpen) deps.setIngestOpen(false);
      setTourVersion(TOUR_VERSION);
      setTourActive(false);
      setTargetResolution(null);
    },
    [deps, restorePanelSnapshot, setTourVersion],
  );

  const finishTour = useCallback(
    () => completeTour({ keepIngestOpen: true }),
    [completeTour],
  );

  const skipTour = useCallback(
    () => completeTour({ keepIngestOpen: false }),
    [completeTour],
  );

  // Unlike completeTour this doesn't touch tourVersion: a viewport shrink
  // pauses the tour rather than completing it, and clearing the latch lets
  // the auto-start effect pick it back up when the window widens again.
  const deferTour = useCallback(() => {
    restorePanelSnapshot();
    deps.setIngestOpen(false);
    deps.setAccountsOpen(false);
    autoStartLatched.current = false;
    setTourActive(false);
    setTargetResolution(null);
  }, [deps, restorePanelSnapshot]);

  const restartTour = useCallback(() => {
    autoStartLatched.current = true;
    panelSnapshot.current = deps.snapshotPanels();
    direction.current = 1;
    setTargetResolution(null);
    setStepIndex(0);
    setTourActive(true);
  }, [deps]);

  const goToStep = useCallback((nextIndex: number, nextDirection: 1 | -1) => {
    direction.current = nextDirection;
    setTargetResolution(null);
    setStepIndex(nextIndex);
  }, []);

  useEffect(() => {
    if (
      autoStartLatched.current ||
      !shouldAutoStartTour({
        authChecked,
        hasUser,
        tourVersion,
        firstListLoadSettled,
        narrowViewport,
      })
    ) {
      return;
    }
    restartTour();
  }, [
    authChecked,
    hasUser,
    tourVersion,
    firstListLoadSettled,
    narrowViewport,
    restartTour,
  ]);

  useEffect(() => {
    if (!tourActive) return;
    const step = TOUR_STEPS[stepIndex];
    if (!step) {
      finishTour();
      return;
    }

    enforceTourPreconditions(step.preconditions, deps);

    // Panel visibility and the ingest disclosure update React state. Resolve
    // the target on the next frame, after those changes have reached the DOM.
    const frame = window.requestAnimationFrame(() => {
      const resolution = resolveTourTarget(step, (selector) =>
        Boolean(document.querySelector(selector)),
      );
      if (resolution.kind !== "skip") {
        setTargetResolution(resolution);
        return;
      }

      const nextIndex = stepIndex + direction.current;
      if (nextIndex < 0) {
        goToStep(0, 1);
      } else if (nextIndex >= TOUR_STEPS.length) {
        finishTour();
      } else {
        goToStep(nextIndex, direction.current);
      }
    });

    return () => window.cancelAnimationFrame(frame);
  }, [deps, finishTour, goToStep, stepIndex, tourActive]);

  return {
    tourActive,
    stepIndex,
    targetResolution,
    restartTour,
    deferTour,
    skipTour,
    finishTour,
    goToStep,
    lockedPopover: tourLockedPopover(tourActive, stepIndex),
  };
}
