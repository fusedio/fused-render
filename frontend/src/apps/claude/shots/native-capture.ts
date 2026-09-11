// THE NATIVE SCREEN SHOT (T:9766-9956) — the pane's LIVE pixels, no document
// needed.
//
// `POST /api/capture/shot-region` is the ScreenCaptureKit / GDI / desktop-portal
// still behind `fused.capture.screenshot()` (SPEC §45), the same one
// `platform/lib/appShot.ts` drives for the export path (AF-11). Every capture
// tries it FIRST: no share prompt (so nothing hinges on a click's transient
// activation, and a walkthrough's tenth note raises no picker), it works in
// whatever browser opened the page, and the pixels are the ones the user is
// looking at — WebGL maps included, which the DOM clone can only report blank.
//
// It photographs VISIBLE pixels, so the frame must be on screen and the window
// on ONE display (the server refuses a straddling rect). Every "cannot" — no
// server, 409 unsupported, 400 off-display, a hidden pane, an origin we cannot
// compute — answers null, never a throw, and the caller falls through to the
// paths that were there before.
import { SHOT_NATIVE_MIN, SHOT_TIMEOUT_MS, type PaneBitmap } from "./types";

/** Set by a 409, or by the boot probe below: "this platform has no still".
 *  Remembered, so a page over an unsupported backend pays the round trip once
 *  (T:9790, 9924) — or, with the probe, not even once. */
let nativeOff = false;

export function isNativeOff(): boolean {
  return nativeOff;
}

export function resetNativeOffForTests(): void {
  nativeOff = false;
}

/**
 * THE BOOT PROBE'S HALF of the same fact (T:7840-7847): `capture.sources()` is
 * read once at boot and `shotNativeOff = true` when
 * `src.screenshot.available === false`, so a platform with no still is known
 * BEFORE the first attempt rather than after a doomed round trip and a
 * `console.warn`.
 *
 * That round trip was the small cost. The real one is T:7688 — `if (annOn &&
 * annXO && shotNativeOff) annXOStreamGet()` — because arming a mode over a
 * cross-origin target is the ONE moment carrying the user activation
 * `getDisplayMedia` needs. Gated on a `nativeOff` that only a live 409 could
 * set, that branch read `false` on the first arm, and the later
 * fire-and-forget capture then asked for a stream with no activation behind
 * it: the first cross-origin walkthrough silently produced no pictures at all.
 *
 * Reads `available === false` and nothing else, exactly as T does — `granted`
 * is deliberately NOT consulted, since on macOS it is the first shot that
 * raises the Screen Recording prompt — so an inconclusive probe leaves the
 * native road open rather than shutting a working feature.
 */
export function noteSourcesProbe(
  sources: { screenshot?: { available?: boolean | null } | null } | null | undefined,
): void {
  if (sources?.screenshot?.available === false) nativeOff = true;
}

/** The attribute the SHELL hides its own overlay chrome on — the same
 *  `SHOOTING_ATTR` appShot.ts stamps (T:9878). */
export const SHOOTING_ATTR = "data-capture-shooting";

/** Where the TOP window's viewport sits on the screen, learned from pointer
 *  events the way appShot.ts learns it: a MouseEvent carries screenX/Y and
 *  clientX/Y both, and their difference IS the viewport origin in screen units —
 *  exact for any chrome layout (a side panel, vertical tabs), where the
 *  outerWidth/innerWidth arithmetic assumes chrome on top split evenly at the
 *  sides. Our window may be a frame of the shell's, so the learned origin is
 *  walked up to the top through the frame offsets (T:9802). */
let topOrigin: { x: number; y: number } | null = null;

/** `pointerdown` ONLY: a keyboard-activated click is a real MouseEvent with
 *  every coordinate ZERO, which would teach an origin of (0,0) and send viewport
 *  coordinates to the server as screen ones (T:9812). */
export function learnTopOrigin(e: { screenX: number; clientX: number; screenY: number; clientY: number }, win: Window): void {
  const off = frameOffset(win);
  if (!off) return;
  topOrigin = { x: e.screenX - e.clientX - off.x, y: e.screenY - e.clientY - off.y };
}

export function topOriginForTests(): { x: number; y: number } | null {
  return topOrigin;
}

export function resetTopOriginForTests(): void {
  topOrigin = null;
}

/**
 * The origin has to be learned from the click that PRECEDES a capture, and
 * capture-phase so a `stopPropagation` in a menu cannot hide it.
 *
 * NOT at module load, which is where this lived until PR2's review: any bundle
 * that so much as imports `shots/*` would then carry a document-wide listener
 * for a feature its page may never turn on — the chat flag off included. It is
 * the CHAT's listener, so the chat's own mount registers it and the returned
 * teardown takes it away again (appShot.ts keeps its copy for the export path).
 */
export function watchTopOrigin(win: Window | null | undefined): () => void {
  const host = win;
  if (!host || typeof host.addEventListener !== "function") return () => {};
  const onDown = (e: Event): void => learnTopOrigin(e as PointerEvent, host);
  host.addEventListener("pointerdown", onDown, { capture: true, passive: true });
  return () => host.removeEventListener("pointerdown", onDown, { capture: true });
}

/** A window's viewport origin within the TOP window's viewport, summed over every
 *  frame element between them; null when a frame on the way is not ours to read
 *  (a cross-origin ancestor — no screen rect can be computed then) (T:9822). */
export function frameOffset(win: Window): { x: number; y: number } | null {
  let x = 0;
  let y = 0;
  try {
    for (let w = win; w !== w.top; w = w.parent) {
      const fe = w.frameElement;
      if (!fe) return null;
      const r = fe.getBoundingClientRect();
      x += r.left + (fe.clientLeft || 0);
      y += r.top + (fe.clientTop || 0);
    }
  } catch {
    return null;
  }
  return { x, y };
}

/** The frame's CONTENT box in screen units, or null when it is off screen, not
 *  fully inside the top viewport (a sliver is a wrong picture, and the server
 *  would refuse the rect anyway) or too small to be worth photographing. The
 *  border is trimmed because the pixels have to be the framed viewport's: the
 *  space every pin and crop rect is already in (T:9838). */
export function screenRect(
  frame: HTMLIFrameElement,
): { rect: [number, number, number, number]; width: number; height: number } | null {
  const win = frame.ownerDocument && frame.ownerDocument.defaultView;
  if (!win) return null;
  const off = frameOffset(win);
  if (!off) return null;
  const r = frame.getBoundingClientRect();
  const w = Math.round(frame.clientWidth || r.width);
  const h = Math.round(frame.clientHeight || r.height);
  if (w < SHOT_NATIVE_MIN.width || h < SHOT_NATIVE_MIN.height) return null;
  const left = off.x + r.left + (frame.clientLeft || 0);
  const top = off.y + r.top + (frame.clientTop || 0);
  let topWin: Window;
  try {
    topWin = win.top as Window;
    void topWin.innerWidth;
  } catch {
    return null;
  }
  if (left < 0 || top < 0 || left + w > topWin.innerWidth || top + h > topWin.innerHeight) {
    return null;
  }
  const origin =
    topOrigin || {
      x: topWin.screenX + Math.max(0, (topWin.outerWidth - topWin.innerWidth) / 2),
      y: topWin.screenY + Math.max(0, topWin.outerHeight - topWin.innerHeight),
    };
  return { rect: [origin.x + left, origin.y + top, w, h], width: w, height: h };
}

export interface NativeCaptureOptions {
  /** What sits ON the pane and must not be in its picture — the annotation layer
   *  (PR3) and the shutter flash. The DOM clone never had this problem
   *  (`cloneNode` skips a shadow tree) but a screen shot sees everything the user
   *  does (T:9884). The default finds the flash, which this module's own
   *  `flash()` stamps `[data-shot-flash]`. */
  overlays?: () => Element[];
}

/** The default overlay set: every flash sheet in this document and in the frame's
 *  owner document. PR3 passes its own, which adds the annotation shadow host.
 *
 *  EXPORTED because the native path is not the only one that photographs what
 *  the user can see: tab capture reads the same pixels off a video track, and it
 *  was burning the white sheet into the picture whenever the grab beat the flash
 *  home (`xo-capture`, Bugbot PR #1064). One finder, so a second overlay this
 *  page learns to hide is hidden on both roads at once.
 *
 *  Every read is guarded: a caller's `frame` may be a stub whose owner document
 *  is not a real one, and a finder that throws would take the whole capture with
 *  it rather than simply hiding nothing. */
export function flashOverlays(frame: HTMLIFrameElement): Element[] {
  const els: Element[] = [];
  const docs = new Set<Document>();
  if (typeof document !== "undefined") docs.add(document);
  try {
    if (frame.ownerDocument) docs.add(frame.ownerDocument);
  } catch {
    /* not ours */
  }
  for (const doc of docs) {
    try {
      if (typeof doc.querySelectorAll !== "function") continue;
      for (const el of Array.from(doc.querySelectorAll("[data-shot-flash]"))) els.push(el);
    } catch {
      /* not queryable */
    }
  }
  return els;
}

/** One native shot of `frame`, at the frame's CSS size. Null for every "cannot"
 *  (T:9905). */
export async function captureNative(
  frame: HTMLIFrameElement | null,
  deadline?: number,
  opts: NativeCaptureOptions = {},
): Promise<PaneBitmap | null> {
  if (nativeOff || !frame || !frame.isConnected) return null;
  const box = screenRect(frame);
  if (!box) return null;
  const hidden = (opts.overlays ? opts.overlays() : flashOverlays(frame)).filter(
    (el): el is HTMLElement => !!(el as HTMLElement).style,
  );
  const prior = hidden.map((el) => el.style.visibility);
  let hostBody: HTMLElement | null = null;
  try {
    hostBody = frame.ownerDocument.body;
  } catch {
    /* not ours */
  }
  try {
    for (const el of hidden) el.style.visibility = "hidden";
    if (hostBody) hostBody.setAttribute(SHOOTING_ATTR, "");
    // Two frames, so the hiding is PAINTED before the screen is read — bounded,
    // because rAF never fires in a hidden document and a hang here would eat the
    // caller's whole deadline (T:9891).
    await Promise.race([
      new Promise<void>((res) =>
        requestAnimationFrame(() => requestAnimationFrame(() => res())),
      ),
      new Promise<void>((res) => setTimeout(res, 120)),
    ]);
    const ctl = new AbortController();
    const left = (deadline || Date.now() + SHOT_TIMEOUT_MS) - Date.now();
    const timer = setTimeout(() => ctl.abort(), Math.max(200, Math.min(4000, left)));
    let res: Response;
    try {
      res = await fetch("/api/capture/shot-region", {
        method: "POST",
        signal: ctl.signal,
        headers: { "X-Fused": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ rect: box.rect, dpr: window.devicePixelRatio || 1 }),
      });
    } finally {
      clearTimeout(timer);
    }
    // 409 is "this platform has no still": remembered. 400 (off-display, bad
    // rect) and 500 are about THIS shot — the next one may land (T:9920).
    if (res.status === 409) nativeOff = true;
    if (!res.ok) {
      // Said, not swallowed: a 400 on every click is a bug in the arithmetic
      // above that only the console can show (T:9926).
      let why = "";
      try {
        const body: unknown = await res.json();
        why = (body as { _error?: string } | null)?._error || "";
      } catch {
        /* not JSON */
      }
      console.warn("native pane shot refused (" + res.status + "): " + why, {
        rect: box.rect,
        dpr: window.devicePixelRatio || 1,
      });
      return null;
    }
    const blob = await res.blob();
    if (!blob.size || !blob.type.startsWith("image/png")) return null;
    const bitmap = await createImageBitmap(blob);
    // Drawn at the frame's CSS size, not the display's: every crop rect and badge
    // position downstream is in the framed viewport's CSS pixels (T:9944).
    const canvas = document.createElement("canvas");
    canvas.width = box.width;
    canvas.height = box.height;
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0, box.width, box.height);
    bitmap.close();
    return {
      canvas,
      width: box.width,
      height: box.height,
      blanks: [],
      styled: 0,
      incomplete: false,
      imagesMissing: 0,
    };
  } catch {
    return null; // unreachable server, aborted, undecodable — the fallbacks answer
  } finally {
    hidden.forEach((el, i) => {
      el.style.visibility = prior[i];
    });
    if (hostBody) hostBody.removeAttribute(SHOOTING_ATTR);
  }
}
