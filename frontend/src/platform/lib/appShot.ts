// One real-pixels screenshot of an app, for the export path (D396): when a
// folder without an authored preview.png is exported, this captures what the
// app actually looks like and downloadAppFile bakes it into the .fused as
// `files/preview.png`.
//
// The mechanism is a NATIVE SCREEN SHOT — `POST /api/capture/shot-region`, the
// same ScreenCaptureKit / GDI / desktop-portal still behind
// `fused.capture.screenshot()` (SPEC §45), pointed at the screen rect the
// browser reports for an element. It replaced tab capture (getDisplayMedia
// with current-tab hints) for three reasons: no share prompt, so nothing
// hinges on the click's transient user activation any more; it works in
// whatever browser `webbrowser.open` chose, where the Chromium-only tab hints
// did not; and the pixels are the same live ones — DOM serialization was
// never an option here because fused apps are map/canvas/WebGL-heavy and an
// SVG-foreignObject rasterization of those is reliably blank, which baked
// into the artifact would be a permanently wrong thumbnail.
//
// A screen shot photographs VISIBLE pixels only, so the app must be on screen
// and the window must be fully on one display (the server refuses a rect that
// is not — a sliver baked in as the permanent thumbnail is a valid PNG nothing
// downstream can catch). Two sources, in preference order (owner call — no
// full-screen flash):
//
//   1. The card's own thumbnail. A card without a preview.png already renders
//      the live app in its thumb (AppPreviewCard's fallback chain), so the
//      pixels the user is looking at ARE the app — shoot that rect. Nothing
//      navigates, nothing flashes; the shot is thumb-sized (~card width ×
//      devicePixelRatio), which is exactly the size the card that will display
//      it renders at.
//   2. A stage (scrim + fresh iframe of the entry under `_preview=1` /
//      `_nofocus=1`), only when no usable thumb element was offered or it is
//      off-screen/too small to be worth photographing. The frame is sized so
//      its shot lands under MAX_SHOT_WIDTH at this DPR; only a crop source
//      that is itself wider than that (the explorer's preview pane) gets
//      re-encoded down (`capWidth`).
//
// Every failure — no server, permission denied, the app failing to load, the
// window straddling displays, an empty body — resolves to undefined, never a
// throw: the caller exports WITHOUT a preview, which is exactly what the
// export did before this existed.
//
// On macOS the first shot on a machine that has not granted Screen Recording
// raises the TCC dialog (capture._darwin: "the prompt rides the first real
// capture"), and THAT export ships plain; the ones after it carry a preview.
import { downloadAppFile } from "./api";
import { thumbUrl } from "./thumb-frame";

// The slice of AppInfo the export path reads — structural, so the preview
// header (which has a folder + entry page but no listing row) can call
// exportAppFile without inventing a fake AppInfo.
export interface ExportableApp {
  path: string;
  name: string;
  entry_html?: string | null;
  preview_image?: string | null;
}

// How long the stage's app gets to paint something worth photographing after
// its frame's load event: boot scripts, first fetch, first map tiles. A
// capture is a one-off user action, so erring generous beats a blank shot.
// The thumb path pays none of this — its pixels are already painted.
const SETTLE_MS = 1500;

// The captured PNG's width cap. A shot lands at the display's own pixel
// scale — a 5k-wide preview.png inside every .fused is waste; card thumbs
// render ~400px wide. The stage sizes its frame from this; a thumb crop is
// always under it; a wider crop source is scaled down to it (`capWidth`).
const MAX_SHOT_WIDTH = 1600;

// The stage frame's shape — the card thumb's own (appfile.MAX_PREVIEW_BYTES
// comment: "a card thumbnail is ~1280x800"), so the baked preview fills the
// slot it will be shown in.
const STAGE_ASPECT = 16 / 10;

// Below this on-screen size a thumb crop would be photographing noise —
// take the stage instead.
const MIN_CROP_CSS_PX = { width: 120, height: 75 };

function shotUrl(entryHtml: string): string {
  return thumbUrl(`/render?path=${encodeURIComponent(entryHtml)}`);
}

// The one export entry every card surface calls (the hover chip and the
// context menu): capture only when there is something to gain — a renderable
// page and no authored preview.png — then the ordinary download. A capture
// that comes back undefined (unsupported, refused, blank) exports plain.
//
// `captureEl` is the no-flash source above, and the CALLER'S CONTRACT on it is
// narrow: an element whose pixels ARE the app *right now*. Not "the box the
// app will render in" — a card thumb whose live iframe has not loaded is an
// empty grey box, and shooting that bakes the empty box into the artifact as
// its permanent thumbnail (a valid PNG, so nothing downstream can catch it).
// The /apps grid admits only two preview iframes at a time
// (preview-start.createPreviewStartQueue(2)), so "mounted but not painted" is
// the COMMON state of a card, not a rare one. A caller that cannot promise
// painted pixels passes nothing and gets the stage, which is the whole reason
// the stage exists. `cropRect` can only check geometry, so it cannot enforce
// this — the promise is made where the state lives.
export async function exportAppFile(
  app: ExportableApp,
  captureEl?: Element | null,
): Promise<void> {
  const preview =
    !app.preview_image && app.entry_html
      ? await captureAppPreview(app.entry_html, captureEl)
      : undefined;
  return downloadAppFile(app.path, app.name, preview);
}

// Whether `el`'s box is fully inside the viewport and big enough that a shot
// of it is a picture of the app rather than a sliver of one. GEOMETRY ONLY —
// whether the element has actually painted the app is the caller's promise
// (see exportAppFile), because nothing in a bounding rect can answer it.
function cropRect(el: Element | null | undefined): DOMRect | null {
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
// preview and the app cut off at the right. The export is always a click
// away, and that click passes through here (capture phase, so a
// stopPropagation in a menu cannot hide it).
let viewportOrigin: { x: number; y: number } | undefined;
if (typeof window !== "undefined") {
  // `pointerdown` only, never `click`: a keyboard-activated click is a real
  // MouseEvent with screenX/clientX all ZERO, which would teach an origin of
  // (0,0) and send viewport coordinates to the server as screen ones. No
  // pointer ever produces a pointerdown, so a keyboard export falls through
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

// Two frames, so whatever the click that reached here was tearing down — the
// context menu over the thumb — has been painted away before the screen is
// photographed. Tab capture never needed this: its grab came long after the
// prompt; a native shot is immediate.
function nextPaint(): Promise<void> {
  return new Promise((res) =>
    requestAnimationFrame(() => requestAnimationFrame(() => res())),
  );
}

// While a shot is being taken the body carries this attribute, and the
// stylesheet hides the overlay UI that sits ON the thumb — the card's hover
// export chip (`.app-pcard-export`, apps.css). Clicking that chip does not end
// the hover, so without this the chip is in the pixels and becomes part of
// the artifact's permanent thumbnail. An attribute rather than a class on
// the card so any surface with overlay UI over its capture source can join
// the same rule.
const SHOOTING_ATTR = "data-capture-shooting";

// A PNG wider than the cap, re-encoded narrower. The thumb and stage paths
// never need this — a card thumb is ~400 CSS px and the stage is sized to
// the cap — but the explorer's preview pane is the crop source there and
// fills whatever the pane is; at 2x on a wide window that is a 4k-wide still,
// which can cross the export route's 8 MiB cap and ship the .fused with no
// preview at all. Decode → canvas → encode only in that case.
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

export async function captureAppPreview(
  entryHtml: string,
  captureEl?: Element | null,
): Promise<Blob | undefined> {
  let stage: HTMLDivElement | undefined;
  try {
    let source: Element;
    if (cropRect(captureEl)) {
      source = captureEl as Element;
    } else {
      // No usable thumb on screen — the stage: a scrim over the whole
      // viewport (so nothing of the page bleeds into the shot's margins) with
      // the app's own page in a frame sized for the shot: as wide as the cap
      // allows at this DPR, no wider than the viewport, thumb-shaped.
      const dpr = window.devicePixelRatio || 1;
      const width = Math.floor(Math.min(window.innerWidth, MAX_SHOT_WIDTH / dpr));
      const height = Math.floor(Math.min(window.innerHeight, width / STAGE_ASPECT));
      stage = document.createElement("div");
      stage.style.cssText =
        "position:fixed;inset:0;z-index:2147483000;background:#fff;" +
        "display:flex;align-items:center;justify-content:center;";
      const frame = document.createElement("iframe");
      frame.src = shotUrl(entryHtml);
      frame.style.cssText =
        `width:${width}px;height:${height}px;border:0;display:block;background:#fff;`;
      frame.tabIndex = -1;
      stage.appendChild(frame);
      document.body.appendChild(stage);
      await new Promise<void>((res) => {
        // load OR error: an app that never loads still resolves — the export
        // must not hang; the blank capture is the price of a broken page.
        frame.addEventListener("load", () => res(), { once: true });
        frame.addEventListener("error", () => res(), { once: true });
        setTimeout(res, 10_000);
      });
      await new Promise((res) => setTimeout(res, SETTLE_MS));
      source = frame;
    }
    document.body.setAttribute(SHOOTING_ATTR, "");
    await nextPaint();
    // Re-read at shoot time: cheap insurance against layout shifting while
    // the stage settled.
    const r = cropRect(source);
    if (!r) return undefined;
    const rect = screenRect(r);
    const res = await fetch("/api/capture/shot-region", {
      method: "POST",
      headers: { "X-Fused": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ rect, dpr: window.devicePixelRatio || 1 }),
    });
    // Errors come back as the JSON `_error` shape (400 bad rect / off-display,
    // 409 unsupported here, 500) — all the same outcome for an export.
    if (!res.ok) return undefined;
    const blob = await res.blob();
    if (!blob.size || !blob.type.startsWith("image/png")) return undefined;
    return await capWidth(blob, MAX_SHOT_WIDTH);
  } catch {
    // Server unreachable, frame refused — all the same outcome: export
    // without a preview.
    return undefined;
  } finally {
    document.body.removeAttribute(SHOOTING_ATTR);
    stage?.remove();
  }
}
