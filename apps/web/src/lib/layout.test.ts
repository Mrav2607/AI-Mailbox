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

describe("loadUi density", () => {
  beforeEach(() => window.localStorage.clear());

  it("accepts both literal choices", () => {
    window.localStorage.setItem(
      UI_KEY,
      JSON.stringify({ ...DEFAULT_UI, density: "compact" }),
    );
    expect(loadUi().density).toBe("compact");

    window.localStorage.setItem(
      UI_KEY,
      JSON.stringify({ ...DEFAULT_UI, density: "comfortable" }),
    );
    expect(loadUi().density).toBe("comfortable");
  });

  it.each(["cozy", 1, null, undefined])(
    "falls back for an invalid value (%s) without poisoning other fields",
    (value) => {
      window.localStorage.setItem(
        UI_KEY,
        JSON.stringify({ ...DEFAULT_UI, density: value, autoSync: 60 }),
      );
      const ui = loadUi();
      expect(ui.density).toBe(DEFAULT_UI.density);
      expect(ui.autoSync).toBe(60);
    },
  );

  it("falls back when an older blob has no density", () => {
    const { density: _density, ...oldUi } = DEFAULT_UI;
    window.localStorage.setItem(UI_KEY, JSON.stringify(oldUi));

    expect(loadUi().density).toBe(DEFAULT_UI.density);
  });
});

describe("loadUi fontScale", () => {
  beforeEach(() => window.localStorage.clear());

  it("accepts any finite number in [0.75, 1.5]", () => {
    for (const value of [0.75, 0.9, 1, 1.1, 1.5]) {
      window.localStorage.setItem(
        UI_KEY,
        JSON.stringify({ ...DEFAULT_UI, fontScale: value }),
      );
      expect(loadUi().fontScale).toBe(value);
    }
  });

  it.each([0.74, 1.51, -1, "1", null, Number.NaN, Number.POSITIVE_INFINITY])(
    "falls back for an invalid value (%s) without poisoning other fields",
    (value) => {
      window.localStorage.setItem(
        UI_KEY,
        JSON.stringify({ ...DEFAULT_UI, fontScale: value, autoSync: 60 }),
      );
      const ui = loadUi();
      expect(ui.fontScale).toBe(DEFAULT_UI.fontScale);
      expect(ui.autoSync).toBe(60);
    },
  );

  it("falls back when an older blob has no fontScale", () => {
    const { fontScale: _fontScale, ...oldUi } = DEFAULT_UI;
    window.localStorage.setItem(UI_KEY, JSON.stringify(oldUi));

    expect(loadUi().fontScale).toBe(DEFAULT_UI.fontScale);
  });
});
