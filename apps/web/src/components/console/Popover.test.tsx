import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Popover } from "./Popover";

let container: HTMLDivElement;
let root: Root;

function renderPopover(open: boolean, onOpenChange: (v: boolean) => void, lockOpen?: boolean) {
  act(() => {
    root.render(
      <Popover
        open={open}
        onOpenChange={onOpenChange}
        lockOpen={lockOpen}
        trigger={<button type="button">trigger</button>}
      >
        <div>panel content</div>
      </Popover>,
    );
  });
}

beforeEach(() => {
  // React 19 requires this flag for `act` to behave; without it we get
  // "not configured to support act" warnings and flaky assertions.
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
});

describe("Popover dismissal", () => {
  it("closes on an outside mousedown when unlocked", () => {
    const onOpenChange = vi.fn();
    renderPopover(true, onOpenChange);

    act(() => {
      document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("closes on Escape when unlocked", () => {
    const onOpenChange = vi.fn();
    renderPopover(true, onOpenChange);

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("ignores an outside mousedown while lockOpen is set", () => {
    const onOpenChange = vi.fn();
    renderPopover(true, onOpenChange, true);

    act(() => {
      document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });

    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("ignores Escape while lockOpen is set", () => {
    const onOpenChange = vi.fn();
    renderPopover(true, onOpenChange, true);

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });

    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("never dismisses on a mousedown inside the popover", () => {
    const onOpenChange = vi.fn();
    renderPopover(true, onOpenChange);

    const panel = container.querySelector("div > div");
    expect(panel).not.toBeNull();
    act(() => {
      panel?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });

    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
