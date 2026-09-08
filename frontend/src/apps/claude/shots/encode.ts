// PIXELS → BYTES (T:9554-9700, T:8033, T:11462-11518).
//
// The budget ladder and nothing else: no DOM walk, no network, no upload. One
// module because every caller shares one WebP latch — the desktop app's
// WKWebView cannot encode WebP and fails SILENTLY (`toBlob(cb,"image/webp",q)`
// hands back a blob whose type is "image/png", byte-identical, no throw, no
// null), so the capability is probed once per page and remembered (T:9611).
import {
  SHOT_MAX_BYTES,
  SHOT_MAX_EDGE,
  SHOT_MIN_AREA,
  SHOT_VIEW_EDGE,
  SHOT_WEBP_QUALITY,
  type PaneBitmap,
  type ShotBadge,
  type ShotRect,
} from "./types";

/** Canvases come from the document in production; a test hands over a fake
 *  through `setCanvasFactory`. */
let canvasFactory: (() => HTMLCanvasElement) | null = null;

export function setCanvasFactory(f: (() => HTMLCanvasElement) | null): void {
  canvasFactory = f;
}

function makeCanvas(): HTMLCanvasElement {
  return canvasFactory ? canvasFactory() : document.createElement("canvas");
}

/** Whether this engine really encodes WebP: null until the first attempt tells
 *  us. Probed once per page, not once per shot — the capability cannot change
 *  mid-session, and re-probing would cost every WKWebView user a wasted encode
 *  on every single shot (T:9631). */
let webpOk: boolean | null = null;

export function webpLatch(): boolean | null {
  return webpOk;
}

export function resetWebpLatchForTests(): void {
  webpOk = null;
}

/** Target size for a shot: longest edge down to `maxEdge`, never UP (a 320px
 *  button blown up to 640 is more bytes and no more information). Edges floor at
 *  1px, because a zero-dimension canvas throws on `toBlob` and would cost the
 *  whole capture rather than one crop (T:9598). */
export function fit(
  w: number,
  h: number,
  maxEdge?: number,
): { width: number; height: number; scale: number } {
  const scale = Math.min(1, (maxEdge || SHOT_MAX_EDGE) / Math.max(w, h));
  return {
    width: Math.max(1, Math.round(w * scale)),
    height: Math.max(1, Math.round(h * scale)),
    scale,
  };
}

/** Integer crop rect inside the pane bitmap, or null when there is nothing to
 *  cut. Origin floors and the far edge ceils — rounding both the same way shaves
 *  the element's last row of pixels, which on a 1px border is the entire point
 *  of the crop (T:9578). */
export function cropRect(
  rect: { left: number; top: number; width: number; height: number },
  paneW: number,
  paneH: number,
): ShotRect | null {
  const l = Math.max(0, Math.floor(rect.left));
  const t = Math.max(0, Math.floor(rect.top));
  const r = Math.min(paneW, Math.ceil(rect.left + rect.width));
  const b = Math.min(paneH, Math.ceil(rect.top + rect.height));
  const w = r - l;
  const h = t < b ? b - t : 0;
  if (w <= 0 || h <= 0 || w * h < SHOT_MIN_AREA) return null;
  return { left: l, top: t, width: w, height: h };
}

export function toBlob(
  canvas: HTMLCanvasElement,
  type?: string,
  quality?: number,
): Promise<Blob | null> {
  return new Promise((res) => canvas.toBlob(res, type, quality));
}

/** The extension a blob has EARNED, read off the bytes rather than off what we
 *  asked the encoder for. A file named `.webp` holding PNG bytes would be worse
 *  than never trying WebP, so the name follows the content (T:9616). */
export function shotExt(blob: Blob | null | undefined): string {
  return blob && blob.type === "image/webp" ? ".webp" : ".png";
}

export interface EncodeLimits {
  maxEdge?: number;
  maxBytes?: number;
}

/** Filled with the size the WINNING encode used — the attach path's chip has to
 *  SAY what it did to the pixels ("downscaled from 4200×2800 to 1600×1067") and
 *  `fit` is only the first guess (T:9648). */
export interface EncodeSize {
  width: number;
  height: number;
}

/** Cut one region out of a pane bitmap and encode it as small as it can be while
 *  staying legible (T:9655).
 *
 *  Knob order is deliberate: quality first, resolution last. Both formats are
 *  tried at each size rather than one being assumed smaller — lossy WebP usually
 *  wins, PNG genuinely beats it on flat UI, and the first encoding that FITS is
 *  the one we want. `null` = over budget however hard we tried; no shot beats one
 *  that dominates the turn. */
export async function encode(
  pane: Pick<PaneBitmap, "canvas">,
  rect: ShotRect,
  limits?: EncodeLimits,
  out?: EncodeSize,
): Promise<Blob | null> {
  const maxEdge = limits?.maxEdge || SHOT_MAX_EDGE;
  const maxBytes = limits?.maxBytes || SHOT_MAX_BYTES;
  let box = fit(rect.width, rect.height, maxEdge);
  const done = (blob: Blob): Blob => {
    if (out) {
      out.width = box.width;
      out.height = box.height;
    }
    return blob;
  };
  for (let attempt = 0; attempt < 3; attempt++) {
    const c = makeCanvas();
    c.width = box.width;
    c.height = box.height;
    c.getContext("2d")?.drawImage(
      pane.canvas,
      rect.left,
      rect.top,
      rect.width,
      rect.height,
      0,
      0,
      box.width,
      box.height,
    );
    const fitted = await ladder(c, maxBytes);
    if (fitted.blob) return done(fitted.blob);
    if (fitted.dead) return null;
    box = halve(box);
  }
  return null;
}

/** `encode`'s ladder plus the badges: drawn AFTER the pane is scaled into the
 *  output canvas and BEFORE encoding, at each badge's `x,y` scaled the same way
 *  the pane was, so every badge lands on its exact spot at every size the loop
 *  tries rather than needing its own retry (T:8033). */
export async function encodeBadged(
  pane: PaneBitmap,
  badges: ShotBadge[],
  limits?: EncodeLimits,
): Promise<Blob | null> {
  const maxEdge = limits?.maxEdge || SHOT_MAX_EDGE;
  const maxBytes = limits?.maxBytes || SHOT_MAX_BYTES;
  let box = fit(pane.width, pane.height, maxEdge);
  for (let attempt = 0; attempt < 3; attempt++) {
    const c = makeCanvas();
    c.width = box.width;
    c.height = box.height;
    const ctx = c.getContext("2d");
    ctx?.drawImage(pane.canvas, 0, 0, pane.width, pane.height, 0, 0, box.width, box.height);
    if (ctx) {
      for (const b of badges) drawBadge(ctx, b.x * box.scale, b.y * box.scale, b.label);
    }
    const fitted = await ladder(c, maxBytes);
    if (fitted.blob) return fitted.blob;
    if (fitted.dead) return null;
    box = halve(box);
  }
  return null;
}

function halve(box: { width: number; height: number; scale: number }) {
  return {
    width: Math.max(1, Math.round(box.width / 2)),
    height: Math.max(1, Math.round(box.height / 2)),
    scale: box.scale / 2,
  };
}

/** One size's worth of the ladder: WebP 0.8 then 0.6 while the latch allows it,
 *  then PNG. `dead` = the encoder answered null, which ends the whole ladder
 *  (T:9673-9686). */
async function ladder(
  c: HTMLCanvasElement,
  maxBytes: number,
): Promise<{ blob: Blob | null; dead: boolean }> {
  if (webpOk !== false) {
    for (const q of SHOT_WEBP_QUALITY) {
      const webp = await toBlob(c, "image/webp", q);
      if (!webp) break;
      if (webp.type !== "image/webp") {
        // The silent failure. Recorded so no later shot pays for the discovery
        // again, and fall through to PNG — which is what this blob already is.
        webpOk = false;
        break;
      }
      webpOk = true;
      if (webp.size <= maxBytes) return { blob: webp, dead: false };
    }
  }
  const png = await toBlob(c, "image/png");
  if (!png) return { blob: null, dead: true };
  if (png.size <= maxBytes) return { blob: png, dead: false };
  return { blob: null, dead: false };
}

/** A labeled badge in CANVAS pixel space — legible however small the picture
 *  ends up, unlike an overlay the picture cannot carry. White ring under a red
 *  disc so it reads on light and dark app backgrounds alike; the LETTER is the
 *  payload, the same string the annotation's `label` carries on the wire
 *  (T:8009). */
export function drawBadge(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  label: string,
): void {
  const r = 11;
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = "#ff2d55";
  ctx.fill();
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
  ctx.fillStyle = "#fff";
  ctx.font = "bold " + (label && label.length > 1 ? 11 : 13) + "px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label || "?", x, y + 0.5);
  ctx.restore();
}

// ── reading a picture the user brought in (T:11462-11518) ────────────────────

/** Pixels plus the release the caller owes (`close()` for a bitmap, a revoke for
 *  the `<img>` path). `canvas` is a valid `drawImage` SOURCE named the way
 *  `encode` reads its pane, so the resize below is a REUSE of the capture ladder
 *  rather than a second copy of it (T:11462). */
export interface ShotPixels {
  canvas: CanvasImageSource;
  width: number;
  height: number;
  free: () => void;
}

/** The pixels of a picture the user brought in, or null when this engine cannot
 *  get at them. Two mechanisms because they fail in different places:
 *  `createImageBitmap` THROWS on a format it does not know, while `<img>` +
 *  `decode()` is what an engine without it still has. The answer is load-bearing
 *  twice — it decides whether the downscale is even available, and whether a
 *  thumbnail would be honest (T:11462). */
export async function shotPixels(file: Blob): Promise<ShotPixels | null> {
  if (typeof createImageBitmap === "function") {
    try {
      const bmp = await createImageBitmap(file);
      if (bmp && bmp.width && bmp.height) {
        return {
          canvas: bmp,
          width: bmp.width,
          height: bmp.height,
          free: () => {
            try {
              bmp.close();
            } catch {
              /* no close */
            }
          },
        };
      }
    } catch {
      /* an engine without it, or a format it refuses — try <img> */
    }
  }
  if (typeof Image !== "function") return null;
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await (img.decode
      ? img.decode()
      : new Promise<void>((res, rej) => {
          img.onload = () => res();
          img.onerror = () => rej(new Error("undecodable"));
        }));
    if (!img.naturalWidth || !img.naturalHeight) throw new Error("no pixels");
    return {
      canvas: img,
      width: img.naturalWidth,
      height: img.naturalHeight,
      free: () => URL.revokeObjectURL(url),
    };
  } catch {
    URL.revokeObjectURL(url);
    return null;
  }
}

/** One oversize picture, re-encoded small enough to attach. The ladder is
 *  `encode`'s and not a new one, capped at SHOT_VIEW_EDGE — a photo the user
 *  brought in is never carried at a size the app's own screenshots are not.
 *  `null` means even the halvings could not reach the budget (T:11518). */
export async function shrink(
  pix: ShotPixels,
  maxBytes: number,
): Promise<{ blob: Blob; width: number; height: number } | null> {
  const size: EncodeSize = { width: 0, height: 0 };
  const blob = await encode(
    pix,
    { left: 0, top: 0, width: pix.width, height: pix.height },
    { maxEdge: SHOT_VIEW_EDGE, maxBytes },
    size,
  );
  return blob ? { blob, width: size.width, height: size.height } : null;
}

/** Re-encode an oversized image found INSIDE a capture down to something worth
 *  embedding: base64 adds a third on top of the bytes and the result is inlined
 *  in markup that then has to be parsed, rasterised and encoded again — so a
 *  4 MB photo would cost more than the whole screenshot it appears in. Drawn
 *  from an object URL rather than the page's own `<img>`, so the canvas is never
 *  tainted (T:9396 shotShrinkImage). */
export async function shrinkImage(blob: Blob): Promise<Blob> {
  const objUrl = URL.createObjectURL(blob);
  try {
    const img = await loadImage(objUrl);
    const box = fit(img.naturalWidth || 1, img.naturalHeight || 1, SHOT_VIEW_EDGE);
    const c = makeCanvas();
    c.width = box.width;
    c.height = box.height;
    c.getContext("2d")?.drawImage(img, 0, 0, box.width, box.height);
    // WebP where it is real, PNG where it silently is not — the same one-line
    // check `shotExt` spells out, for the same WKWebView reason (T:9414).
    const webp = await toBlob(c, "image/webp", 0.8);
    if (webp && webp.type === "image/webp") return webp;
    return (await toBlob(c, "image/png")) || blob;
  } finally {
    URL.revokeObjectURL(objUrl);
  }
}

export function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error("the image could not be decoded"));
    img.src = src;
  });
}

/** A blob as a `data:` URL. FileReader rather than manual base64: the only path
 *  that does not push the whole file through a JS string byte by byte (T:9375). */
export function dataUrl(blob: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result || ""));
    fr.onerror = () => rej(new Error("the image bytes could not be read"));
    fr.readAsDataURL(blob);
  });
}

/** Whether a canvas read back nothing we can use. maplibre and deck.gl create
 *  their WebGL context with `preserveDrawingBuffer:false`, so `toDataURL` hands
 *  back a fully TRANSPARENT image of the right size — and an agent that believes
 *  the app is blank spends a whole debugging loop on a bug that does not exist.
 *  Compared against a same-size EMPTY canvas's encoding: PNG of a transparent
 *  W×H is deterministic, so equality is the test. "Cannot tell" is unreadable,
 *  not fine (T:9355). */
export function readbackIsBlank(url: string, blankUrl: string): boolean {
  return !url || !blankUrl || url === blankUrl;
}

/** Replace each `<canvas>` in the clone with an `<img>` of its own pixels: a
 *  `<canvas>` serializes as an empty element, so without this every chart and map
 *  in the capture is a hole. Returns the SOURCE canvases whose pixels were not
 *  readable, for the caveat that reports them (T:9554). */
export function rasterise(src: Element, dst: Element): Element[] {
  const cs = src.querySelectorAll("canvas");
  const cd = dst.querySelectorAll("canvas");
  const blanks: Element[] = [];
  const probe = src.ownerDocument.createElement("canvas");
  for (let i = 0; i < cs.length && i < cd.length; i++) {
    let url = "";
    try {
      url = cs[i].toDataURL("image/png");
    } catch {
      url = "";
    }
    let blankUrl = "";
    try {
      probe.width = cs[i].width;
      probe.height = cs[i].height;
      blankUrl = probe.toDataURL("image/png");
    } catch {
      blankUrl = "";
    }
    if (readbackIsBlank(url, blankUrl)) {
      // Left as a <canvas> in the clone, which serializes empty — the same hole
      // it would have been anyway. The caveat is what reports it.
      blanks.push(cs[i]);
      continue;
    }
    const img = dst.ownerDocument.createElement("img");
    img.setAttribute("src", url);
    img.setAttribute("style", cd[i].getAttribute("style") || "");
    cd[i].replaceWith(img);
  }
  return blanks;
}
