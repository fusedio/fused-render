// ONE PLACE THAT SAYS "THIS FRAME IS A PICTURE". Every display-only thumbnail
// the shell mounts — an /apps or Home card, a bookmark peek, the staging frame
// the export capture photographs — is described here and nowhere else.
//
// It exists because the description is not one thing but five, and they were
// spread across three files and four call sites: two URL stamps the frame's own
// runtime reads, the sandbox/permissions seal the browser enforces, and the
// small markup that keeps the frame out of the tab order and off the scrollbar.
// A card that got four of the five was the bug (D348 shipped `_preview` without
// `_nofocus`; D496 found `_nofocus` without inheritance), and a fifth thumbnail
// surface would have had to remember all of them from scratch.
//
// What each part is for is written where it is defined: the stamps in
// frame-focus.ts (`_nofocus`) and router.ts (`_preview`), the seal in
// frame-focus.ts (THUMB_SEAL). This module is the assembly, not the argument.
//
// Router-importing, which is why it is not IN frame-focus.ts: that module is
// deliberately router-free and DOM-free so the rules stay pinnable by a test
// with neither, and `withPreviewFlag` lives in a router that reads `location`
// at module init.
import { THUMB_SEAL, withNoFocus } from "./frame-focus";
import { withPreviewFlag } from "./router";

// The URL of a page being rendered as a picture: it records no open (D301) and
// takes no keyboard (D496). For a caller that needs the address rather than a
// whole frame — the export capture's staging frame builds its own element.
export function thumbUrl(src: string): string {
  return withNoFocus(withPreviewFlag(src));
}

// Everything a thumbnail <iframe> needs except its own layout. Spread it first
// and let the caller's `style`/`onLoad`/`onError` follow:
//
//   <iframe {...thumbFrame(liveSrc)} style={…} onLoad={…} />
//
// `tabIndex: -1` keeps it out of the tab order (a picture is not a stop);
// `scrolling: "no"` keeps the reader from scrolling it and drops the
// scrollbars a scaled-down page would otherwise show; `title: ""` because the
// box is `aria-hidden` decoration and an announced frame title would contradict
// that. Clicks are already the card's: both stylesheets set `pointer-events:
// none` on the thumbnail iframe.
export function thumbFrame(src: string) {
  return {
    src: thumbUrl(src),
    tabIndex: -1,
    scrolling: "no",
    title: "",
    ...THUMB_SEAL,
  } as const;
}
