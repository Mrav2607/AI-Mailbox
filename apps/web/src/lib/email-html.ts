/**
 * Turning a hostile email into something safe to look at.
 *
 * Two layers, on purpose. DOMPurify strips the markup we know is dangerous, and
 * then the result is dropped into a sandboxed iframe with its own CSP — an
 * opaque origin that can't reach localStorage (where the session token lives) or
 * the console's DOM. The sanitizer is the layer that's *expected* to work; the
 * frame is the layer that has to hold when it doesn't.
 */
import DOMPurify from "dompurify";

// Strip scripts/handlers and keep <style> out so email CSS can't bleed into the
// console. Inline style attributes survive (emails lean on them heavily).
export const PURIFY_CONFIG = {
  USE_PROFILES: { html: true },
  FORBID_TAGS: [
    "style",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "form",
    "input",
    "button",
    "textarea",
    "select",
  ],
};

// Anything that can pull a remote URL out of a style attribute. url() is the
// obvious one; image-set() is the one a regex-only defence forgets — CSS treats
// the bare string inside it as a URL, so a tracking pixel sails straight
// through a /url\(/ check. We can't win a parser fight with CSS from here, which
// is exactly why the frame's CSP does the actual enforcing. This only decides
// whether to show the "remote images blocked" banner.
const REMOTE_IN_STYLE = /url\s*\(|image-set\s*\(|https?:|(?:^|[\s:(])\/\//i;

export function sanitizeEmailHtml(
  html: string,
  allowRemote: boolean,
): { html: string; blocked: boolean } {
  let blocked = false;
  const sanitizeAttributes = (node: Element) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
    if (!allowRemote) {
      for (const attribute of ["src", "srcset", "poster", "background"]) {
        const value = node.getAttribute(attribute)?.trim();
        if (value && /^(?:https?:|\/\/)/i.test(value)) {
          node.removeAttribute(attribute);
          blocked = true;
        }
      }
      if (REMOTE_IN_STYLE.test(node.getAttribute("style") ?? "")) {
        node.removeAttribute("style");
        blocked = true;
      }
    }
  };

  DOMPurify.addHook("afterSanitizeAttributes", sanitizeAttributes);
  try {
    return { html: DOMPurify.sanitize(html, PURIFY_CONFIG), blocked };
  } finally {
    DOMPurify.removeHook("afterSanitizeAttributes", sanitizeAttributes);
  }
}

// The frame's own policy, layered on top of the page CSP it inherits (an
// about:srcdoc document inherits its parent's). Both policies apply, so this can
// only ever tighten things — which is the point: a URL grammar the browser
// enforces, instead of a regex we have to keep ahead of.
export function framePolicy(allowRemote: boolean): string {
  const img = allowRemote ? "data: https:" : "data:";
  return [
    "default-src 'none'",
    `img-src ${img}`,
    "style-src 'unsafe-inline'", // emails are inline styles all the way down
    "form-action 'none'",
    "base-uri 'none'",
  ].join("; ");
}

// A system font stack, deliberately: the inherited page CSP blocks cross-origin
// stylesheets and fonts, so anything web-hosted would just fail to load.
const FRAME_STYLE = `
  html { overflow-y: auto; }
  body {
    margin: 0; padding: 12px 16px;
    background: #fff; color: #171717;
    font: 13px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
    overflow-wrap: break-word;
  }
  img { max-width: 100%; height: auto; }
  table { max-width: 100%; }
  a { color: #1d4ed8; }
`;

export function emailDocument(bodyHtml: string, allowRemote: boolean): string {
  return [
    "<!doctype html><html><head><meta charset='utf-8'>",
    `<meta http-equiv="Content-Security-Policy" content="${framePolicy(allowRemote)}">`,
    `<style>${FRAME_STYLE}</style></head><body>`,
    bodyHtml,
    "</body></html>",
  ].join("");
}
