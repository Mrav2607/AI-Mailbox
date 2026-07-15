import { beforeEach, describe, expect, it } from "vitest";

import { DEFAULT_UI, loadUi, UI_KEY } from "./layout";

function storeTourVersion(tourVersion: unknown) {
  window.localStorage.setItem(
    UI_KEY,
    JSON.stringify({ ...DEFAULT_UI, tourVersion }),
  );
}

describe("loadUi tourVersion", () => {
  beforeEach(() => window.localStorage.clear());

  it("accepts finite non-negative numbers", () => {
    storeTourVersion(3);
    expect(loadUi().tourVersion).toBe(3);

    storeTourVersion(0.5);
    expect(loadUi().tourVersion).toBe(0.5);
  });

  it.each([-1, "1", null, Number.NaN, Number.POSITIVE_INFINITY])(
    "falls back for an invalid value (%s)",
    (value) => {
      storeTourVersion(value);
      expect(loadUi().tourVersion).toBe(DEFAULT_UI.tourVersion);
    },
  );

  it("falls back when an older blob has no tourVersion", () => {
    const { tourVersion: _tourVersion, ...oldUi } = DEFAULT_UI;
    window.localStorage.setItem(UI_KEY, JSON.stringify(oldUi));

    expect(loadUi().tourVersion).toBe(DEFAULT_UI.tourVersion);
  });
});
