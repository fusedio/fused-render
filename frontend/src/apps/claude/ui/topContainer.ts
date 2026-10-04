// Resolves the portal target for a modal that must be WIDE — spanning the
// whole app window rather than whatever small pane its own script happens to
// be running inside.
//
// This bundle mounts a SECOND (or third) time as the document inside a genuine
// same-origin iframe (a Panel pane, a Tab, the canvases workspace's foreign
// embed — `platform/lib/router.ts`'s `IS_EMBED` family is the established
// framing-detection idiom for exactly this). `ChatMount` never nests a
// `<ClaudeChat>` inside an iframe it renders itself, but the iframe boundary,
// when one exists, sits ABOVE `ChatMount`'s own tree — the whole shell page is
// loaded again inside the frame — so a plan card mounted at one of those sites
// still needs its modal to cover the OUTER window, not the iframe's own
// viewport.
//
// Read LAZILY, at the moment a modal opens, rather than once at module init
// like router.ts's flags: "framed or not" cannot change after load, but the
// top document's `<body>` is not guaranteed to exist yet at THIS module's own
// eval time (a slow outer shell). Any cross-origin access throws — window.top
// is opaque across origins — and the safe default, exactly like router.ts, is
// to behave as though this document is not framed at all: return null and let
// the caller fall back to its own document's body.
export function topDocumentBody(): Element | null {
  try {
    if (typeof window === "undefined") return null;
    const top = window.top;
    if (!top || top === window) return null; // not framed
    const body = top.document?.body;
    if (!body) return null;
    mirrorStylesheets(document, top.document);
    return body;
  } catch {
    return null; // cross-origin (or a test shim with no `.top`) — cannot see it
  }
}

const MIRRORED_ATTR = "data-c-mirrored";

// The chat's CSS (`.chat-root`, `.c-tokens`, the plan-modal layout, hljs) is
// loaded into the document this bundle runs in — the IFRAME. A dialog portaled
// into the top document would render unstyled there unless that document
// happens to have loaded the same sheets (a lazy chunk's CSS lands only in the
// document that imported it). So copy this document's stylesheets across,
// idempotently: a `<link>` is skipped when the target already has one with the
// same href, an inline `<style>` (dev server) when one with identical text
// exists. The copies are tagged and left in place — they are the same rules the
// top document would otherwise lack, and re-opening finds them already there.
export function mirrorStylesheets(from: Document, to: Document): void {
  const target = to.head ?? to.documentElement;
  if (!target) return;
  const seenHref = new Set<string>();
  const seenText = new Set<string>();
  to.querySelectorAll("link[rel~='stylesheet'], style").forEach((n) => {
    if (n.tagName === "LINK") seenHref.add((n as HTMLLinkElement).href);
    else seenText.add(n.textContent ?? "");
  });
  from.querySelectorAll("link[rel~='stylesheet'], style").forEach((n) => {
    if (n.hasAttribute(MIRRORED_ATTR)) return;
    if (n.tagName === "LINK") {
      const href = (n as HTMLLinkElement).href;
      if (!href || seenHref.has(href)) return;
      seenHref.add(href);
      const link = to.createElement("link");
      link.rel = "stylesheet";
      link.href = href;
      link.setAttribute(MIRRORED_ATTR, "");
      target.appendChild(link);
    } else {
      const text = n.textContent ?? "";
      if (!text || seenText.has(text)) return;
      seenText.add(text);
      const style = to.createElement("style");
      style.textContent = text;
      style.setAttribute(MIRRORED_ATTR, "");
      target.appendChild(style);
    }
  });
}

export default topDocumentBody;
