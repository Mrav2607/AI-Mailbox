import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LandingPage } from "./LandingPage";

let container: HTMLDivElement;
let root: Root;
let writeText: ReturnType<typeof vi.fn>;

function renderLanding(onSignIn: () => void = vi.fn()) {
  act(() => {
    root.render(<LandingPage onSignIn={onSignIn} theme="dark" onTheme={vi.fn()} />);
  });
}

function findButtonByText(text: string): HTMLButtonElement | null {
  return (
    (Array.from(container.querySelectorAll("button")).find(
      (btn) => btn.textContent?.trim() === text,
    ) as HTMLButtonElement | undefined) ?? null
  );
}

function click(el: Element) {
  act(() => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  // navigator.clipboard doesn't exist in jsdom by default -- stub it so the
  // quick-start copy button has something to call.
  writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("LandingPage sections", () => {
  it("renders the header/main/footer landmarks and one h1", () => {
    renderLanding();

    expect(container.querySelector("header")).not.toBeNull();
    expect(container.querySelector("main")).not.toBeNull();
    expect(container.querySelector("footer")).not.toBeNull();
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("h1")?.textContent).toBe(
      "Your inbox, triaged by your own model.",
    );
  });

  it("renders the how-it-works, labels, agenda, features and quick-start copy", () => {
    renderLanding();

    const text = container.textContent ?? "";
    expect(text).toContain(
      "Connect multiple Gmail and Outlook accounts; every message lands in one place, sorted into the six labels.",
    );
    expect(text).toContain("Six labels. A closed set: the model never invents a seventh.");
    expect(text).toContain(
      "Action items get pulled out of every account you connect and collected into one list, sorted by due date.",
    );
    expect(text).toContain("Keyboard-first console");
    expect(text).toContain("Running in two commands");
    expect(text).toContain("CortexMail: self-hosted email triage.");
  });

  it("shows only the six primary labels in the labels section, no agenda/all/unclassified/done", () => {
    renderLanding();

    const labelsSection = container.querySelector("#taxonomy-heading")?.closest("section");
    expect(labelsSection).not.toBeNull();
    const text = labelsSection!.textContent ?? "";

    for (const name of ["needs reply", "action req", "fyi", "promo", "security", "spam"]) {
      expect(text).toContain(name);
    }
    for (const name of ["agenda", "all", "unclassified", "done"]) {
      expect(text).not.toContain(name);
    }
  });

  it("renders the sections in order: how it works, labels, agenda, features, quick start", () => {
    renderLanding();

    const headingIds = Array.from(
      container.querySelectorAll("main section[aria-labelledby]"),
    ).map((section) => section.getAttribute("aria-labelledby"));

    expect(headingIds).toEqual([
      "how-it-works-heading",
      "taxonomy-heading",
      "agenda-heading",
      "features-heading",
      "quick-start-heading",
    ]);
  });

  it("renders the console mockup as decorative with a visible caption", () => {
    renderLanding();

    const mockup = container.querySelector('[aria-hidden="true"]');
    expect(mockup).not.toBeNull();
    expect(container.textContent).toContain(
      "The triage console: every thread labeled, with the model's confidence next to it.",
    );
  });
});

describe("LandingPage sign-in", () => {
  it("fires onSignIn from the nav ghost button", () => {
    const onSignIn = vi.fn();
    renderLanding(onSignIn);

    const navButton = findButtonByText("Sign in");
    expect(navButton).not.toBeNull();
    click(navButton!);

    expect(onSignIn).toHaveBeenCalledTimes(1);
  });

  it("fires onSignIn from the primary hero CTA", () => {
    const onSignIn = vi.fn();
    renderLanding(onSignIn);

    const heroButton = findButtonByText("Sign in →");
    expect(heroButton).not.toBeNull();
    click(heroButton!);

    expect(onSignIn).toHaveBeenCalledTimes(1);
  });
});

describe("LandingPage quick-start copy button", () => {
  it("writes the commands to the clipboard and announces success", async () => {
    renderLanding();

    const copyButton = findButtonByText("copy");
    expect(copyButton).not.toBeNull();

    await act(async () => {
      copyButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(
      "cp deploy/local.env.example deploy/.env\ndocker compose up --build",
    );

    const status = container.querySelector('[role="status"]');
    expect(status?.textContent).toBe("Copied to clipboard.");
  });
});
