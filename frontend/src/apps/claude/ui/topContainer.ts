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
    return body ?? null;
  } catch {
    return null; // cross-origin (or a test shim with no `.top`) — cannot see it
  }
}

export default topDocumentBody;
