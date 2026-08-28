// One picture of an app, for the export path and for "Set current view as
// preview" (D396).
//
// The mechanism is a DOM CLONE (D621) — the same technique the Claude chat
// template uses, ported to `domShot.ts`: clone the app document's body, inline
// every computed style, put the scroll offsets back, inline the `<img>`s as
// data: URLs, rasterise the `<canvas>`es, serialize to an SVG `<foreignObject>`
// and draw that on a canvas.
//
// THE OWNER REVERTED THE NATIVE SCREEN SHOT to get here. `POST
// /api/capture/shot-region` (#889) photographed real screen pixels, and this
// file no longer calls it at all. What the clone buys back:
//   nothing has to be ON SCREEN. A screen shot photographs visible pixels, so
//     the app had to be on screen and the window fully on one display; a clone
//     reads the DOM, so neither constrains it.
//   no OS permission and no TCC dialog. On macOS the first native shot on a
//     machine without Screen Recording raised a system prompt and THAT export
//     shipped plain.
//   no screen-geometry arithmetic. The native path had to map a viewport rect
//     to screen coordinates, which a browser with a SIDE PANEL (Arc's sidebar,
//     vertical tabs) skewed — the shell's own sidebar baked into the preview.
//     A clone works in the frame's own coordinates, so there is nothing to map,
//     and the `pointerdown` listener that learned the viewport origin is gone
//     with it.
//
// AND THE KNOWN COST, plainly: a WebGL or `<canvas>` pane RASTERISES BLANK.
// maplibre/deck.gl create their context with `preserveDrawingBuffer: false`, so
// the pixels cannot be read back and the picture shows the app's background
// where the map was. fused apps are map- and canvas-heavy, so this is not a
// rare case — for those apps the baked preview is the page's chrome around an
// empty rectangle. That is the trade the owner accepted for a capture that
// needs no permission, no on-screen window and no share prompt.
//
// Two sources, in preference order (owner call — no full-screen flash):
//
//   1. The card's own thumbnail iframe. A card without a preview.png already
//      renders the live app in its thumb (AppPreviewCard's fallback chain), so
//      the document in that frame IS the app — clone that. Nothing navigates,
//      nothing flashes.
//   2. A stage (scrim + fresh iframe of the entry under `_preview=1` /
//      `_nofocus=1`), only when no usable thumb frame was offered. The frame is
//      sized so its picture lands under MAX_SHOT_WIDTH at this DPR.
//
// Both are same-origin `/render?...` pages, which is what makes the clone
// possible at all: a cross-origin frame exposes no `contentDocument`, and that
// resolves to undefined like any other failure.
//
// Every failure — a cross-origin frame, an empty body, markup that will not
// rasterise, the app failing to load — resolves to undefined, never a throw:
// the caller exports WITHOUT a preview, which is exactly what the export did
// before this existed.
import { downloadAppFile } from "./api";
import { domShot } from "./domShot";
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
// its frame's load event: boot scripts, first fetch, first map tiles. A capture
// is a one-off user action, so erring generous beats a blank picture. The thumb
// path pays none of this — its document is already rendered.
const SETTLE_MS = 1500;

// The captured PNG's width cap. A clone is drawn at the display's own pixel
// scale — a 5k-wide preview.png inside every .fused is waste; card thumbs
// render ~400px wide. The stage sizes its frame from this, and a wider source
// is scaled down to it (`capWidth`).
const MAX_SHOT_WIDTH = 1600;

// The stage frame's shape — the card thumb's own (appfile.MAX_PREVIEW_BYTES
// comment: "a card thumbnail is ~1280x800"), so the baked preview fills the
// slot it will be shown in.
const STAGE_ASPECT = 16 / 10;

// Below this on-screen size a crop would be photographing noise — take the
// stage instead.
const MIN_CROP_CSS_PX = { width: 120, height: 75 };

function shotUrl(entryHtml: string): string {
  return thumbUrl(`/render?path=${encodeURIComponent(entryHtml)}`);
}

// The one export entry every card surface calls (the hover chip and the context
// menu): capture only when there is something to gain — a renderable page and
// no authored preview.png — then the ordinary download. A capture that comes
// back undefined (cross-origin, blank, unrasterisable) exports plain.
//
// `captureEl` is the no-flash source above, and the CALLER'S CONTRACT on it is
// narrow: an element whose document IS the app *right now*. Not "the box the app
// will render in" — a card thumb whose live iframe has not loaded has an EMPTY
// body, and cloning that would bake an empty box into the artifact as its
// permanent thumbnail (a valid PNG, so nothing downstream can catch it). The
// /apps grid admits only two preview iframes at a time
// (preview-start.createPreviewStartQueue(2)), so "mounted but not painted" is
// the COMMON state of a card, not a rare one. A caller that cannot promise a
// rendered document passes nothing and gets the stage, which is the whole reason
// the stage exists. `cropRect` can only check geometry, so it cannot enforce
// this — the promise is made where the state lives. (domShot's own empty-body
// check is a floor under it, not a substitute: a frame can hold a rendered
// error page.)
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

// Whether `el`'s box is big enough that a picture of it is a picture of the app
// rather than a sliver of one. GEOMETRY ONLY — whether the frame has actually
// rendered the app is the caller's promise (see exportAppFile), because nothing
// in a bounding rect can answer it.
//
// NOTE what is no longer checked: the native shot needed the element fully
// inside the viewport, because it photographed the screen and an off-screen
// element had no pixels. A DOM clone reads the frame's document, so a partly
// scrolled-out card clones exactly as well as a centred one — the viewport
// containment test is gone, and with it the "the app frame has to be fully on
// screen" failure it caused. Size is still worth checking: it is the frame's own
// layout size that the picture is drawn at.
export function cropRect(el: Element | null | undefined): DOMRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width < MIN_CROP_CSS_PX.width || r.height < MIN_CROP_CSS_PX.height) return null;
  return r;
}

// Two frames, so whatever the click that reached here was tearing down — the
// context menu over the thumb — has been painted away before the clone is
// taken. It matters less than it did for a screen shot (a clone of the app's
// own frame cannot contain the shell's menu at all), but the SHOOTING_ATTR
// below still has to take effect before the clone, and that is a style change.
function nextPaint(): Promise<void> {
  return new Promise((res) =>
    requestAnimationFrame(() => requestAnimationFrame(() => res())),
  );
}

// While a capture is being taken the body carries this attribute, and the
// stylesheet hides the overlay UI that sits ON the thumb — the card's hover
// export chip (`.app-pcard-export`, apps.css). An attribute rather than a class
// on the card so any surface with overlay UI over its capture source can join
// the same rule.
//
// A clone of the app's own document cannot include the shell's chip, so this no
// longer guards the pixels the way it did for a screen shot; it is kept because
// the chip sits over the frame the user is watching and flashing it away for the
// duration is the same feedback it always was.
const SHOOTING_ATTR = "data-capture-shooting";

// The iframe a capture source stands for: the element itself, or the one live
// frame inside it. The card export hands over the `.app-pcard-thumb` SPAN (the
// frame is its child), the explorer hands over the preview pane's frame
// directly — one lookup serves both, and a wrapper with no frame in it is
// simply not a source (Bugbot on #919: the span passed `cropRect`, so the
// stage never ran and the .fused shipped with no preview and no error).
export function frameOf(el: Element): HTMLIFrameElement | null {
  if (el instanceof HTMLIFrameElement) return el;
  return el.querySelector("iframe");
}

// The frame's window, or null when its document cannot be read. A cross-origin
// frame exposes no `contentDocument` (and touching `contentWindow.document`
// throws), which is the one case a DOM clone simply cannot serve.
function frameWindow(el: Element): Window | null {
  const frame = frameOf(el);
  if (!frame) return null;
  try {
    // contentDocument is null for cross-origin; check it before the window so
    // the same-origin test is a null check rather than a caught throw.
    if (!frame.contentDocument) return null;
    return frame.contentWindow;
  } catch {
    return null;
  }
}

// A PNG wider than the cap, re-encoded narrower. The stage is already sized to
// the cap, but the explorer's preview PANE is the source there and fills
// whatever the pane is; at 2x on a wide window that is a 4k-wide picture, which
// can cross the export route's 8 MiB cap and ship the .fused with no preview at
// all. Decode → canvas → encode only in that case.
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

function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob | undefined> {
  return new Promise((res) => canvas.toBlob((b) => res(b ?? undefined), "image/png"));
}

// Clone `frame`'s document at the frame's own CSS size × devicePixelRatio,
// capped so the result lands under MAX_SHOT_WIDTH, and encode it as a PNG.
// undefined for anything that stops it: not an iframe, cross-origin, empty
// body, markup that will not rasterise.
async function shootFrame(source: Element): Promise<Blob | undefined> {
  // Measured on the FRAME, not the wrapper: a thumb span's padding or border
  // would otherwise be drawn as document that is not there.
  const frame = frameOf(source);
  if (!frame) return undefined;
  const r = cropRect(frame);
  if (!r) return undefined;
  const win = frameWindow(frame);
  if (!win) return undefined;
  const dpr = window.devicePixelRatio || 1;
  // Never scale the picture UP past the cap: at 2x a wide preview pane would
  // otherwise be drawn at 4k and then thrown away again by capWidth.
  const scale = Math.min(dpr, MAX_SHOT_WIDTH / Math.max(1, r.width));
  const shot = await domShot(win, {
    width: r.width,
    height: r.height,
    scale,
    hostDocument: document,
  });
  if (!shot) return undefined;
  return canvasToPng(shot.canvas);
}

export async function captureAppPreview(
  entryHtml: string,
  captureEl?: Element | null,
  // `stage: false` — clone the shown frame or nothing. The caller is asking for
  // THIS view (the explorer's "Set current view as preview"), and a fresh reload
  // of the entry on a stage is a different picture than the one the user is
  // looking at; silently saving it would be a lie with a success toast (Bugbot,
  // 2026-08-27). The export path keeps the stage: any picture beats no thumbnail
  // in a .fused.
  opts: { stage?: boolean } = {},
): Promise<Blob | undefined> {
  let stage: HTMLDivElement | undefined;
  try {
    let source: Element;
    // The offered element counts only if there is a FRAME in it worth drawing:
    // the card hands over its thumb span, so the frame is looked up first and
    // the geometry test runs on the frame, not the wrapper.
    const live = captureEl ? frameOf(captureEl) : null;
    if (live && cropRect(live)) {
      source = live;
    } else if (opts.stage === false) {
      return undefined;
    } else {
      // No usable frame offered — the stage: a scrim over the viewport with the
      // app's own page in a frame sized for the picture. The scrim no longer has
      // to keep the page out of a photograph (a clone of the frame's document
      // cannot contain it), but the frame still has to be LAID OUT to be cloned
      // at a sensible size, so it is a real on-screen element.
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
    const blob = await shootFrame(source);
    if (!blob || !blob.size) return undefined;
    return await capWidth(blob, MAX_SHOT_WIDTH);
  } catch {
    // Cross-origin, unrasterisable markup, a frame that never loaded — all the
    // same outcome: export without a preview.
    return undefined;
  } finally {
    document.body.removeAttribute(SHOOTING_ATTR);
    stage?.remove();
  }
}
