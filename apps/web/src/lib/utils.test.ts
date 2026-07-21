import { describe, expect, it } from "vitest";

import { gmailThreadUrl } from "./utils";

describe("gmailThreadUrl", () => {
  it("links to the default signed-in account without an accountEmail", () => {
    expect(gmailThreadUrl("18c2f1a")).toBe(
      "https://mail.google.com/mail/#all/18c2f1a",
    );
  });

  it("targets a specific Google account via authuser when given one", () => {
    expect(gmailThreadUrl("18c2f1a", "ops-archive@gmail.com")).toBe(
      "https://mail.google.com/mail/?authuser=ops-archive%40gmail.com#all/18c2f1a",
    );
  });

  it("encodes both the thread id and the account email", () => {
    expect(gmailThreadUrl("thread/with spaces", "a b@gmail.com")).toBe(
      "https://mail.google.com/mail/?authuser=a%20b%40gmail.com#all/thread%2Fwith%20spaces",
    );
  });
});
