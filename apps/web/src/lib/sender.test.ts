import { describe, expect, it } from "vitest";

import { senderName } from "./sender";

describe("senderName", () => {
  it("uses the display name before an angle address", () => {
    expect(senderName("Display Name <addr@example.com>")).toBe("Display Name");
  });

  it("strips quotes around a display name", () => {
    expect(senderName('"Display Name" <addr@example.com>')).toBe("Display Name");
  });

  it("uses the local part of a bare email address", () => {
    expect(senderName("addr@example.com")).toBe("addr");
  });

  it("returns null for empty and missing senders", () => {
    expect(senderName("")).toBeNull();
    expect(senderName(null)).toBeNull();
  });
});
