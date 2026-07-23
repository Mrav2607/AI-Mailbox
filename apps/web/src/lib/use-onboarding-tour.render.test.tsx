import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { Panels } from "@/App";
import { TOUR_VERSION } from "@/lib/layout";
import { TOUR_STEPS } from "@/lib/tour-steps";
import { useOnboardingTour, type TourDeps } from "@/lib/use-onboarding-tour";

// Every selector any step in TOUR_STEPS might resolve to, so the rAF target
// resolution never falls through to skip mid-test. This suite pins the
// hook's own step-transition and popover-lifecycle logic, not Popover's
// real open/closed DOM (that's covered by Agent B's Popover.test.tsx).
const FIXTURE_SELECTORS = [
  "topbar-sync",
  "bucket-sidebar",
  "bucket-needs_reply",
  "thread-list",
  "search",
  "sort",
  "detail-pane",
  "prediction",
  "layout",
  "accounts",
  "accounts-panel",
  "ingest-panel",
];

function mountFixtures(): HTMLElement[] {
  return FIXTURE_SELECTORS.map((tour) => {
    const el = document.createElement("div");
    el.setAttribute("data-tour", tour);
    document.body.appendChild(el);
    return el;
  });
}

const ACCOUNTS_INDEX = TOUR_STEPS.findIndex((step) => step.slug === "accounts");
const INGEST_INDEX = TOUR_STEPS.findIndex((step) => step.slug === "ingest");
const SEARCH_INDEX = TOUR_STEPS.findIndex((step) => step.slug === "search");

describe("useOnboardingTour rendered lifecycle", () => {
  let root: Root;
  let container: HTMLElement;
  let fixtures: HTMLElement[];
  let calls: string[];
  let deps: TourDeps;
  let setTourVersion: Mock<(version: number) => void>;
  let api: ReturnType<typeof useOnboardingTour> | null;

  function Harness() {
    api = useOnboardingTour({
      authChecked: true,
      hasUser: false, // gate off — this suite drives the tour by hand
      tourVersion: 0,
      firstListLoadSettled: true,
      narrowViewport: false,
      deps,
      setTourVersion,
    });
    return null;
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    // Runs the step-entry resolution synchronously instead of on the next
    // paint, and must return a numeric handle so cancelAnimationFrame stays
    // happy on unmount/cleanup.
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});

    fixtures = mountFixtures();
    calls = [];
    // Built once per test, not per render: the step-entry effect keys on
    // deps identity, so a fresh object each render would reschedule
    // resolution in a loop instead of settling.
    deps = {
      showPanel: (panel) => calls.push(`show:${panel}`),
      setBucket: (bucket) => calls.push(`bucket:${bucket}`),
      setIngestOpen: (open) => calls.push(`ingest:${open}`),
      setAccountsOpen: (open) => calls.push(`accounts:${open}`),
      snapshotPanels: vi.fn<() => Panels>(
        () => ({ sidebar: true, detail: true, prediction: true }),
      ),
      restorePanels: vi.fn<(snapshot: Panels) => void>(),
    };
    setTourVersion = vi.fn<(version: number) => void>();
    api = null;

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root.render(<Harness />);
    });
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    fixtures.forEach((el) => el.remove());
    vi.unstubAllGlobals();
  });

  function start() {
    act(() => {
      api!.restartTour();
    });
  }

  function goTo(index: number, direction: 1 | -1 = 1) {
    act(() => {
      api!.goToStep(index, direction);
    });
  }

  it("accounts opens accounts + closes ingest; forward to ingest reverses; back to accounts reverses again", () => {
    start();
    calls.length = 0;

    goTo(ACCOUNTS_INDEX);
    expect(calls).toEqual(["ingest:false", "accounts:true"]);
    expect(api!.lockedPopover).toBe("accounts");

    calls.length = 0;
    goTo(INGEST_INDEX);
    expect(calls).toEqual(["accounts:false", "ingest:true"]);
    expect(api!.lockedPopover).toBe("ingest");

    calls.length = 0;
    goTo(ACCOUNTS_INDEX, -1);
    expect(calls).toEqual(["ingest:false", "accounts:true"]);
    expect(api!.lockedPopover).toBe("accounts");
  });

  it("skipTour closes both popovers and writes TOUR_VERSION", () => {
    start();
    goTo(ACCOUNTS_INDEX);
    calls.length = 0;

    act(() => {
      api!.skipTour();
    });

    expect(calls).toEqual(["accounts:false", "ingest:false"]);
    expect(setTourVersion).toHaveBeenCalledWith(TOUR_VERSION);
    expect(api!.tourActive).toBe(false);
  });

  it("finishTour closes accounts but leaves ingest untouched, and writes TOUR_VERSION", () => {
    start();
    goTo(INGEST_INDEX);
    calls.length = 0;

    act(() => {
      api!.finishTour();
    });

    expect(calls).toEqual(["accounts:false"]);
    expect(setTourVersion).toHaveBeenCalledWith(TOUR_VERSION);
    expect(api!.tourActive).toBe(false);
  });

  it("deferTour closes both popovers and writes nothing", () => {
    start();
    goTo(ACCOUNTS_INDEX);
    calls.length = 0;

    act(() => {
      api!.deferTour();
    });

    expect(calls).toEqual(["ingest:false", "accounts:false"]);
    expect(setTourVersion).not.toHaveBeenCalled();
    expect(api!.tourActive).toBe(false);
  });

  it("stepping past the last step lands finish semantics (accounts closed, ingest untouched, version written)", () => {
    start();
    goTo(INGEST_INDEX);
    calls.length = 0;

    goTo(TOUR_STEPS.length);

    expect(calls).toEqual(["accounts:false"]);
    expect(setTourVersion).toHaveBeenCalledWith(TOUR_VERSION);
    expect(api!.tourActive).toBe(false);
  });

  it("lockedPopover is null for a plain step and for an inactive tour", () => {
    start();
    goTo(SEARCH_INDEX);
    expect(api!.lockedPopover).toBeNull();

    act(() => {
      api!.skipTour();
    });
    expect(api!.lockedPopover).toBeNull();
  });
});
