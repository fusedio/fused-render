// One real-pixels screenshot of an app, taken ONLY on an explicit ask — the
// explorer's "Set Current View as Preview" (Preview.tsx), which writes the
// result to the folder as its authored `preview.png`.
//
// This used to also run implicitly on Share (D396): a folder without a
// preview.png got a native shot baked into the .fused / the public link at
// export time, from the /apps card thumb or a full-viewport stage. Retired
// (2026-09-18, owner call): the shot depended on the Screen Recording grant,
// the window sitting fully on one display, page zoom at 100%, a pointer-
// learned viewport origin and overlay UI hiding itself in time — each of
// which failed at least once and baked a WRONG picture into a permanent
// artifact (a valid PNG nothing downstream can catch). App Doctor's
// `preview` check now surfaces the missing thumbnail as a fact the owner
// fixes on purpose; Share ships whatever preview.png the folder has, or none.
//
// The mechanism is a NATIVE SCREEN SHOT — `POST /api/capture/shot-region`, the
// same ScreenCaptureKit / GDI / desktop-portal still behind
// `fused.capture.screenshot()` (SPEC §45), pointed at the screen rect the
// browser reports for an element. Not DOM serialization: fused apps are
// map/canvas/WebGL-heavy and an SVG-foreignObject rasterization of those is
// reliably blank. Not tab capture: it needs a share prompt and Chromium-only
// hints. A screen shot photographs VISIBLE pixels only, so the element must be
// on screen and the window fully on one display (the server refuses a rect
// that is not).
//
// On macOS the first shot on a machine that has not granted Screen Recording
// raises the TCC dialog (capture._darwin: "the prompt rides the first real
// capture"), and THAT shot comes back undefined.

// The captured PNG's width cap. A shot lands at the display's own pixel
// scale — a 5k-wide preview.png is waste; card thumbs render ~400px wide. A
// wider crop source is scaled down to it (`capWidth`).
const MAX_SHOT_WIDTH = 1600;

// Below this on-screen size a crop would be photographing noise.
const MIN_CROP_CSS_PX = { width: 120, height: 75 };

// Whether `el`'s box is fully inside the viewport and big enough that a shot
// of it is a picture of the app rather than a sliver of one. GEOMETRY ONLY —
// whether the element has actually painted the app is the caller's promise,
// because nothing in a bounding rect can answer it.
export function cropRect(el: Element | null | undefined): DOMRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width < MIN_CROP_CSS_PX.width || r.height < MIN_CROP_CSS_PX.height) return null;
  if (r.left < 0 || r.top < 0 || r.right > window.innerWidth || r.bottom > window.innerHeight) {
    return null;
  }
  return r;
}

// Where the viewport's origin sits on the screen, learned from POINTER EVENTS:
// every MouseEvent carries both `screenX/Y` and `clientX/Y`, and their
// difference IS the viewport origin in screen units — exact for any window
// chrome layout. The arithmetic it replaced (`screenX` + half of
// `outerWidth - innerWidth` per side) assumed chrome sits on top and splits
// evenly at the sides, and a browser with a SIDE PANEL — Arc's sidebar,
// Chrome's side panel, vertical tabs — puts all of it on one side: the shot
// landed half a sidebar too far left, the shell's own sidebar baked into the
// preview and the app cut off at the right. The capture is always a click
// away, and that click passes through here (capture phase, so a
// stopPropagation in a menu cannot hide it).
let viewportOrigin: { x: number; y: number } | undefined;
if (typeof window !== "undefined") {
  // `pointerdown` only, never `click`: a keyboard-activated click is a real
  // MouseEvent with screenX/clientX all ZERO, which would teach an origin of
  // (0,0) and send viewport coordinates to the server as screen ones. No
  // pointer ever produces a pointerdown, so a keyboard capture falls through
  // to the outer/inner arithmetic below instead.
  window.addEventListener(
    "pointerdown",
    (e: PointerEvent) => {
      viewportOrigin = { x: e.screenX - e.clientX, y: e.screenY - e.clientY };
    },
    { capture: true, passive: true },
  );
}

// A viewport rect as the SCREEN sees it, in the browser's own screen units
// (CSS pixels of the screen — points on macOS, DIPs elsewhere; the server
// applies `dpr` where its display measures in physical pixels). The
// outer/inner fallback is for a call no pointer event preceded (keyboard
// activation) and keeps the top-chrome assumption only. Page zoom ≠ 100%
// skews both silently (a wrong crop, still a valid PNG) — accepted.
function screenRect(r: DOMRect): [number, number, number, number] {
  const origin = viewportOrigin ?? {
    x: window.screenX + Math.max(0, (window.outerWidth - window.innerWidth) / 2),
    y: window.screenY + Math.max(0, window.outerHeight - window.innerHeight),
  };
  return [origin.x + r.left, origin.y + r.top, r.width, r.height];
}

// A PNG wider than the cap, re-encoded narrower. The explorer's preview pane
// is the crop source and fills whatever the pane is; at 2x on a wide window
// that is a 4k-wide still, which can cross the preview route's 8 MiB cap.
// Decode → canvas → encode only in that case.
async function capWidth(blob: Blob, maxWidth: number): Promise<Blob | undefined> {
  const bitmap = await createImageBitmap(blob);
  try {
    if (bitmap.width <= maxWidth) return blob;
    const scale = maxWidth / bitmap.width;
    const canvas = document.createElement("canvas");
    canvas.width = maxWidth;
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return undefined;
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob | undefined>((res) =>
      canvas.toBlob((b) => res(b ?? undefined), "image/png"),
    );
  } finally {
    bitmap.close();
  }
}

// Photograph `captureEl` — the shown element, or nothing. The caller is asking
// for THIS view, and a fresh reload of the entry elsewhere would be a different
// picture than the one the user is looking at; silently saving it would be a
// lie with a success toast (Bugbot, 2026-08-27). Undefined on any failure:
// off-screen / too small, server unreachable, refused rect, non-PNG.
export async function captureAppPreview(
  captureEl: Element | null | undefined,
): Promise<Blob | undefined> {
  try {
    const r = cropRect(captureEl);
    if (!r) return undefined;
    const rect = screenRect(r);
    const res = await fetch("/api/capture/shot-region", {
      method: "POST",
      headers: { "X-Fused": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ rect, dpr: window.devicePixelRatio || 1 }),
    });
    // Errors come back as the JSON `_error` shape (400 bad rect / off-display,
    // 409 unsupported here, 500) — all the same outcome: no preview.
    if (!res.ok) return undefined;
    const blob = await res.blob();
    if (!blob.size || !blob.type.startsWith("image/png")) return undefined;
    return await capWidth(blob, MAX_SHOT_WIDTH);
  } catch {
    return undefined;
  }
}
