import { describe, expect, it, vi } from "vitest";

import type { Panels } from "@/App";
import { TOUR_VERSION } from "./layout";
import {
  resolveTourTarget,
  tourLockedPopover,
  TOUR_STEPS,
} from "./tour-steps";
import { shouldSuppressConsoleHotkeys } from "./use-hotkeys";
import {
  enforceTourPreconditions,
  shouldAutoStartTour,
  type TourDeps,
} from "./use-onboarding-tour";

// Literal pin (not TOUR_VERSION - 1 derived below): the point of this test is
// to fail loudly if a future bump to layout.ts's TOUR_VERSION isn't matched
// by a real version-3 tour update.
describe("TOUR_VERSION", () => {
  it("is pinned at 3 for the agenda/AI-settings tour update", () => {
    expect(TOUR_VERSION).toBe(3);
  });
});

describe("shouldAutoStartTour", () => {
  const ready = {
    authChecked: true,
    hasUser: true,
    tourVersion: TOUR_VERSION - 1,
    firstListLoadSettled: true,
    narrowViewport: false,
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
    { narrowViewport: true },
  ])("does not start when a gate fails: %o", (patch) => {
    expect(shouldAutoStartTour({ ...ready, ...patch })).toBe(false);
  });

  // Literal values, not TOUR_VERSION-derived, so a forgotten version bump
  // (the exact class of bug the version gate exists to prevent) fails here.
  it.each([
    { tourVersion: 0, expected: true },
    { tourVersion: 1, expected: true },
    { tourVersion: 2, expected: true },
    { tourVersion: 3, expected: false },
    { tourVersion: 4, expected: false },
  ])("stored tourVersion $tourVersion starts=$expected", ({ tourVersion, expected }) => {
    expect(shouldAutoStartTour({ ...ready, tourVersion })).toBe(expected);
  });
});

describe("shouldSuppressConsoleHotkeys", () => {
  it("suppresses app hotkeys while the tour owns the screen", () => {
    expect(shouldSuppressConsoleHotkeys(false, false, true)).toBe(true);
    expect(shouldSuppressConsoleHotkeys(false, false, false)).toBe(false);
  });
});

function makeDeps(calls: string[]): TourDeps {
  return {
    showPanel: (panel) => calls.push(`show:${panel}`),
    setBucket: (bucket) => calls.push(`bucket:${bucket}`),
    setIngestOpen: (open) => calls.push(`ingest:${open}`),
    setAccountsOpen: (open) => calls.push(`accounts:${open}`),
    showBucketsView: () => calls.push("view:buckets"),
    snapshotPanels: vi.fn<() => Panels>(() => ({
      sidebar: true,
      detail: true,
      prediction: true,
    })),
    restorePanels: vi.fn<(snapshot: Panels) => void>(),
  };
}

describe("tour preconditions", () => {
  it("enforces the prediction step in declaration order, closing both popovers", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const prediction = TOUR_STEPS.find((step) => step.slug === "predictions");

    enforceTourPreconditions(prediction?.preconditions ?? [], deps);

    expect(calls).toEqual(["ingest:false", "accounts:false", "show:detail", "show:prediction"]);
  });

  it("closes both popovers and switches to buckets view for the search step", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const search = TOUR_STEPS.find((step) => step.slug === "search");

    enforceTourPreconditions(search?.preconditions ?? [], deps);

    expect(calls).toEqual(["ingest:false", "accounts:false", "view:buckets"]);
  });

  it("enforces the focus-bucket step in declaration order: sidebar, buckets view, then bucket select", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const focusBucket = TOUR_STEPS.find((step) => step.slug === "focus-bucket");

    enforceTourPreconditions(focusBucket?.preconditions ?? [], deps);

    expect(calls).toEqual([
      "ingest:false",
      "accounts:false",
      "show:sidebar",
      "view:buckets",
      "bucket:needs_reply",
    ]);
  });

  it("enforces the agenda step: sidebar visible, no buckets-view switch", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const agenda = TOUR_STEPS.find((step) => step.slug === "agenda");

    enforceTourPreconditions(agenda?.preconditions ?? [], deps);

    expect(calls).toEqual(["ingest:false", "accounts:false", "show:sidebar"]);
  });

  it("opens accounts and closes ingest for the accounts step", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const accounts = TOUR_STEPS.find((step) => step.slug === "accounts");

    enforceTourPreconditions(accounts?.preconditions ?? [], deps);

    expect(calls).toEqual(["ingest:false", "accounts:true"]);
  });

  it("opens ingest and closes accounts for the ingest step", () => {
    const calls: string[] = [];
    const deps = makeDeps(calls);
    const ingest = TOUR_STEPS.find((step) => step.slug === "ingest");

    enforceTourPreconditions(ingest?.preconditions ?? [], deps);

    expect(calls).toEqual(["accounts:false", "ingest:true"]);
  });
});

describe("tourLockedPopover", () => {
  it("returns null when the tour is inactive", () => {
    expect(tourLockedPopover(false, 10)).toBeNull();
  });

  it("returns null for an out-of-range step index", () => {
    expect(tourLockedPopover(true, TOUR_STEPS.length)).toBeNull();
  });

  it("returns null for a plain step with no locked popover", () => {
    const index = TOUR_STEPS.findIndex((step) => step.slug === "search");
    expect(tourLockedPopover(true, index)).toBeNull();
  });

  it("returns 'accounts' for the accounts step", () => {
    const index = TOUR_STEPS.findIndex((step) => step.slug === "accounts");
    expect(tourLockedPopover(true, index)).toBe("accounts");
  });

  it("returns 'ingest' for the ingest step", () => {
    const index = TOUR_STEPS.findIndex((step) => step.slug === "ingest");
    expect(tourLockedPopover(true, index)).toBe("ingest");
  });
});

describe("resolveTourTarget", () => {
  it("keeps the approved map at 14 auto-placed steps", () => {
    expect(TOUR_STEPS).toHaveLength(14);
    expect(TOUR_STEPS.every((step) => step.placement === "auto")).toBe(true);
  });

  it("prefers the open ingest panel over its topbar anchor", () => {
    const ingest = TOUR_STEPS.find((step) => step.slug === "ingest");
    expect(ingest).toBeDefined();

    expect(resolveTourTarget(ingest!, () => true)).toEqual({
      kind: "target",
      selector: '[data-tour="ingest-panel"]',
    });
  });

  it("prefers the open accounts panel over its trigger anchor", () => {
    const accounts = TOUR_STEPS.find((step) => step.slug === "accounts");
    expect(accounts).toBeDefined();

    expect(resolveTourTarget(accounts!, () => true)).toEqual({
      kind: "target",
      selector: '[data-tour="accounts-panel"]',
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

  describe("agenda fallback chain", () => {
    const agenda = TOUR_STEPS.find((step) => step.slug === "agenda")!;

    it("targets the agenda entry when present", () => {
      expect(resolveTourTarget(agenda, () => true)).toEqual({
        kind: "target",
        selector: '[data-tour="agenda-entry"]',
      });
    });

    it("falls back to the bucket sidebar when the agenda entry is missing", () => {
      expect(
        resolveTourTarget(
          agenda,
          (selector) => selector === '[data-tour="bucket-sidebar"]',
        ),
      ).toEqual({ kind: "target", selector: '[data-tour="bucket-sidebar"]' });
    });

    it("skips when both the agenda entry and the sidebar are missing", () => {
      expect(resolveTourTarget(agenda, () => false)).toEqual({ kind: "skip" });
    });
  });

  describe("ai-settings fallback chain", () => {
    const aiSettings = TOUR_STEPS.find((step) => step.slug === "ai-settings")!;

    it("targets the llm-settings button when present", () => {
      expect(resolveTourTarget(aiSettings, () => true)).toEqual({
        kind: "target",
        selector: '[data-tour="llm-settings"]',
      });
    });

    it("falls back to the accounts panel when llm-settings is missing", () => {
      expect(
        resolveTourTarget(
          aiSettings,
          (selector) => selector === '[data-tour="accounts-panel"]',
        ),
      ).toEqual({ kind: "target", selector: '[data-tour="accounts-panel"]' });
    });

    it("centers when both llm-settings and the accounts panel are missing", () => {
      expect(resolveTourTarget(aiSettings, () => false)).toEqual({ kind: "center" });
    });
  });
});
