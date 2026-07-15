import { describe, expect, it, vi } from "vitest";

import { TOUR_VERSION } from "./layout";
import { resolveTourTarget, TOUR_STEPS } from "./tour-steps";
import { shouldSuppressConsoleHotkeys } from "./use-hotkeys";
import {
  enforceTourPreconditions,
  shouldAutoStartTour,
  type TourDeps,
} from "./use-onboarding-tour";

describe("shouldAutoStartTour", () => {
  const ready = {
    authChecked: true,
    hasUser: true,
    tourVersion: TOUR_VERSION - 1,
    firstListLoadSettled: true,
  };

  it("starts only when every gate is ready", () => {
    expect(shouldAutoStartTour(ready)).toBe(true);
  });

  it.each([
    { authChecked: false },
    { hasUser: false },
    { tourVersion: TOUR_VERSION },
    { tourVersion: TOUR_VERSION + 1 },
    { firstListLoadSettled: false },
  ])("does not start when a gate fails: %o", (patch) => {
    expect(shouldAutoStartTour({ ...ready, ...patch })).toBe(false);
  });
});

describe("shouldSuppressConsoleHotkeys", () => {
  it("suppresses app hotkeys while the tour owns the screen", () => {
    expect(shouldSuppressConsoleHotkeys(false, false, true)).toBe(true);
    expect(shouldSuppressConsoleHotkeys(false, false, false)).toBe(false);
  });
});

describe("tour preconditions", () => {
  it("enforces the prediction step in declaration order", () => {
    const calls: string[] = [];
    const deps: TourDeps = {
      showPanel: (panel) => calls.push(`show:${panel}`),
      setBucket: (bucket) => calls.push(`bucket:${bucket}`),
      openIngestDialog: () => calls.push("ingest"),
      snapshotPanels: vi.fn(() => ({
        sidebar: true,
        detail: true,
        prediction: true,
      })),
      restorePanels: vi.fn(),
    };
    const prediction = TOUR_STEPS.find((step) => step.slug === "predictions");

    enforceTourPreconditions(prediction?.preconditions ?? [], deps);

    expect(calls).toEqual(["show:detail", "show:prediction"]);
  });
});

describe("resolveTourTarget", () => {
  it("keeps the approved map at 11 auto-placed steps", () => {
    expect(TOUR_STEPS).toHaveLength(11);
    expect(TOUR_STEPS.every((step) => step.placement === "auto")).toBe(true);
  });

  it("prefers the open ingest form over its topbar anchor", () => {
    const ingest = TOUR_STEPS.find((step) => step.slug === "ingest");
    expect(ingest).toBeDefined();

    expect(resolveTourTarget(ingest!, () => true)).toEqual({
      kind: "target",
      selector: '[data-tour="topbar-sync"] form',
    });
  });

  it("falls back from the bucket row to the sidebar", () => {
    const bucket = TOUR_STEPS.find((step) => step.slug === "focus-bucket");
    expect(bucket).toBeDefined();

    expect(
      resolveTourTarget(
        bucket!,
        (selector) => selector === '[data-tour="bucket-sidebar"]',
      ),
    ).toEqual({
      kind: "target",
      selector: '[data-tour="bucket-sidebar"]',
    });
  });

  it("resolves absent targets to skip or center as declared", () => {
    const sidebar = TOUR_STEPS.find((step) => step.slug === "buckets");
    const welcome = TOUR_STEPS.find((step) => step.slug === "welcome");

    expect(resolveTourTarget(sidebar!, () => false)).toEqual({ kind: "skip" });
    expect(resolveTourTarget(welcome!, () => false)).toEqual({ kind: "center" });
  });
});
