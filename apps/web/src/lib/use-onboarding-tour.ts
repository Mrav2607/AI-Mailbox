import { useCallback, useEffect, useRef, useState } from "react";

import type { Panels } from "@/App";
import { TOUR_VERSION } from "@/lib/layout";
import type { BucketKey } from "@/lib/types";
import {
  resolveTourTarget,
  TOUR_STEPS,
  type TourPrecondition,
  type TourTargetResolution,
} from "@/lib/tour-steps";

type PanelKey = keyof Panels;

export interface TourDeps {
  showPanel: (panel: PanelKey) => void;
  setBucket: (slug: BucketKey) => void;
  openIngestDialog: () => void;
  snapshotPanels: () => Panels;
  restorePanels: (snapshot: Panels) => void;
}

interface AutoStartGate {
  authChecked: boolean;
  hasUser: boolean;
  tourVersion: number;
  firstListLoadSettled: boolean;
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
    gate.firstListLoadSettled
  );
}

export function enforceTourPreconditions(
  preconditions: TourPrecondition[],
  deps: TourDeps,
): void {
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
        deps.openIngestDialog();
        break;
    }
  }
}

export function useOnboardingTour({
  authChecked,
  hasUser,
  tourVersion,
  firstListLoadSettled,
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

  const endTour = useCallback(() => {
    restorePanelSnapshot();
    setTourVersion(TOUR_VERSION);
    setTourActive(false);
    setTargetResolution(null);
  }, [restorePanelSnapshot, setTourVersion]);

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
    restartTour,
  ]);

  useEffect(() => {
    if (!tourActive) return;
    const step = TOUR_STEPS[stepIndex];
    if (!step) {
      endTour();
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
        endTour();
      } else {
        goToStep(nextIndex, direction.current);
      }
    });

    return () => window.cancelAnimationFrame(frame);
  }, [deps, endTour, goToStep, stepIndex, tourActive]);

  return {
    tourActive,
    stepIndex,
    targetResolution,
    restartTour,
    skipTour: endTour,
    finishTour: endTour,
    goToStep,
  };
}
