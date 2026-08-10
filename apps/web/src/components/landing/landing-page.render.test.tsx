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
      "Connect multiple Gmail and Outlook accounts. Every message lands in one place, sorted into the six labels.",
    );
    expect(text).toContain("The model categorizes each message into a fixed set of six labels.");
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
      "The triage console has every thread labeled, with the model's confidence next to it.",
    );
  });

  it("renders six console rows, one per label", () => {
    renderLanding();

    const rows = container.querySelectorAll('ul[aria-hidden="true"] > li');
    expect(rows).toHaveLength(6);
    const text = container.textContent ?? "";
    expect(text).toContain("New sign-in from Chrome on Ubuntu");
    expect(text).toContain("You've been selected for a $500 reward");
  });

  it("exposes the console's label filter bar as real, focusable buttons", () => {
    renderLanding();

    const chips = container.querySelectorAll(
      '[aria-label="Preview: filter the console by label"] button',
    );
    expect(chips).toHaveLength(6);
    for (const chip of Array.from(chips)) {
      expect(chip.getAttribute("aria-pressed")).toBe("false");
    }
  });

  it("highlights the matching console row on chip focus and toggles it sticky on click", () => {
    renderLanding();

    const chip = container.querySelector(
      'button[aria-label="Filter preview by spam"]',
    ) as HTMLButtonElement | null;
    expect(chip).not.toBeNull();

    const rows = Array.from(container.querySelectorAll('ul[aria-hidden="true"] > li'));
    const spamRow = rows.find((li) => li.textContent?.includes("$500 reward")) as
      | HTMLLIElement
      | undefined;
    const otherRow = rows.find((li) => li.textContent?.includes("Sprint notes")) as
      | HTMLLIElement
      | undefined;
    expect(spamRow).toBeDefined();
    expect(otherRow).toBeDefined();

    // Keyboard focus stands in for hover here -- this test harness has no
    // testing-library fireEvent helpers, and React's onMouseEnter is
    // polyfilled from bubbling mouseover/mouseout rather than a real
    // "mouseenter" event, so a raw dispatchEvent wouldn't reach it. Focus is
    // the other input the spec calls out ("hovering OR keyboard-focusing")
    // and exercises the same activeLabel state.
    act(() => {
      chip!.focus();
    });
    expect(spamRow!.className).toContain("ring-1");
    expect(otherRow!.className).toContain("opacity-45");

    act(() => {
      chip!.blur();
    });
    expect(spamRow!.className).not.toContain("ring-1");
    expect(otherRow!.className).not.toContain("opacity-45");

    click(chip!);
    expect(chip!.getAttribute("aria-pressed")).toBe("true");
    expect(spamRow!.className).toContain("ring-1");

    click(chip!);
    expect(chip!.getAttribute("aria-pressed")).toBe("false");
    expect(spamRow!.className).not.toContain("ring-1");
  });

  it("renders the agenda mockup inside the agenda section with a due-group header and sample items", () => {
    renderLanding();

    const agendaSection = container.querySelector("#agenda-heading")?.closest("section");
    expect(agendaSection).not.toBeNull();

    const mockups = agendaSection!.querySelectorAll('[aria-hidden="true"]');
    expect(mockups.length).toBeGreaterThan(0);

    const text = agendaSection!.textContent ?? "";
    expect(text).toContain("Overdue");
    expect(text).toContain("Pay fall tuition installment");
    expect(text).toContain("RSVP: team offsite Thursday");
  });

  it("shows the operator section heading and the hero boot line", () => {
    renderLanding();

    const text = container.textContent ?? "";
    expect(text).toContain("Built for the operator");
    expect(text).toContain(
      "cortexmail up — 3 accounts · 128 threads · model local:email-classifier",
    );
  });

  it("shows two distinct account chips on the agenda mockup", () => {
    renderLanding();

    const agendaSection = container.querySelector("#agenda-heading")?.closest("section");
    expect(agendaSection).not.toBeNull();

    const workChip = agendaSection!.querySelector('[title="work@acme.io"]');
    const personalChip = agendaSection!.querySelector('[title="personal@gmail.com"]');
    expect(workChip).not.toBeNull();
    expect(personalChip).not.toBeNull();
    expect(workChip?.textContent).toBe("work");
    expect(personalChip?.textContent).toBe("personal");
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
