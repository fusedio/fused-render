// THE CROSS-ORIGIN PANE'S PICTURE: tab capture, cropped to the frame
// (T:9706-9764).
//
// The DOM clone needs a readable document and a cross-origin target has none
// (D349). What the browser can still hand over is the TAB's own pixels:
// `getDisplayMedia` with `preferCurrentTab`, one frame grabbed off the video
// track and cropped to the marked iframe's rect — the same box the overlay (and
// so every point coordinate) lives in, so the crops and the whole-pane shot line
// up with the pins for free.
//
// The stream is KEPT between captures: the share prompt is paid once, not once
// per note — a walkthrough clicking ten spots must not raise ten pickers.
// Released when the user ends the share (`ended`), when the target stops being
// cross-origin, and on pagehide.
import type { PaneBitmap } from "./types";

let stream: MediaStream | null = null;

/** Stop the kept stream. Idempotent — both `ended` and an explicit teardown
 *  reach it (T:9731 annXOStreamStop). */
export function stopStream(): void {
  const s = stream;
  stream = null;
  if (!s) return;
  for (const t of s.getTracks()) {
    try {
      t.stop();
    } catch {
      /* already ended */
    }
  }
}

export function currentStream(): MediaStream | null {
  return stream;
}

/**
 * THE TEARDOWN THE HEADER PROMISES. A kept stream is the browser's "sharing
 * this tab" indicator, and an indicator that survives leaving the chat is the
 * page claiming to watch a screen nobody is looking at any more — so the share
 * ends on `pagehide` (the navigation away, and the only event a discarded page
 * reliably gets) and on the chat's own unmount, which is what the returned
 * teardown is for.
 *
 * Registered from the chat's MOUNT rather than at module load, for the reason
 * `watchTopOrigin` is: any bundle that so much as imports `shots/*` would
 * otherwise carry a listener for a feature its page never turns on (T:9731
 * `annXOStreamStop`).
 */
export function watchStreamTeardown(win: Window | null | undefined): () => void {
  const host = win;
  if (!host || typeof host.addEventListener !== "function") return stopStream;
  const onHide = (): void => stopStream();
  host.addEventListener("pagehide", onHide);
  return () => {
    host.removeEventListener("pagehide", onHide);
    stopStream();
  };
}

/** The kept stream, or a fresh share. Chromium's current-tab hints preselect
 *  THIS tab and offer no switching and no monitors: the crop is only ever
 *  computed against this tab's own layout, so any other surface would be cropped
 *  wrong — the hints make the right choice the one-click one (they are ignored,
 *  not fatal, where unsupported) (T:9706). */
export async function getStream(): Promise<MediaStream> {
  if (stream && stream.getVideoTracks().some((t) => t.readyState === "live")) {
    return stream;
  }
  stopStream();
  stream = await navigator.mediaDevices.getDisplayMedia({
    video: { displaySurface: "browser" },
    audio: false,
    // Not in TS's `DisplayMediaStreamOptions` yet — Chromium-only hints the spec
    // has not caught up with, and the reason this cast exists rather than `any`.
    preferCurrentTab: true,
    selfBrowserSurface: "include",
    surfaceSwitching: "exclude",
    monitorTypeSurfaces: "exclude",
  } as DisplayMediaStreamOptions);
  const track = stream.getVideoTracks()[0];
  if (track) track.addEventListener("ended", stopStream, { once: true });
  return stream;
}

/** One frame of the tab, cropped to `frame`'s rect and drawn at its CSS size.
 *
 *  Same answer shape as the other two paths, so every consumer runs unchanged: a
 *  capture off live pixels has no style walk, no image inlining and no WebGL
 *  readback to caveat, so the doubt fields are simply clean. Null (not a throw)
 *  for every "cannot" (T:9735). */
export async function captureXO(
  frame: HTMLIFrameElement | null,
  hostWin?: Window | null,
): Promise<PaneBitmap | null> {
  if (!frame || !navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    return null;
  }
  let host: Window;
  try {
    host = (hostWin || frame.ownerDocument.defaultView) as Window;
    void host.innerWidth;
  } catch {
    return null;
  }
  if (!host) return null;
  const live = await getStream();
  const video = document.createElement("video");
  video.muted = true;
  video.srcObject = live;
  await video.play();
  // One PAINTED frame — `play()` resolving does not mean pixels arrived (T:9748).
  if (video.requestVideoFrameCallback) {
    await new Promise<void>((res) => video.requestVideoFrameCallback(() => res()));
  } else {
    await new Promise<void>((res) => setTimeout(res, 150));
  }
  // The captured frame is the tab's viewport at the capture's own resolution; the
  // iframe's rect in that viewport, scaled the same way, is the crop (T:9752).
  const sx = video.videoWidth / host.innerWidth;
  const sy = video.videoHeight / host.innerHeight;
  const r = frame.getBoundingClientRect();
  const w = Math.max(1, Math.round(r.width));
  const h = Math.max(1, Math.round(r.height));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas
    .getContext("2d")
    ?.drawImage(video, r.left * sx, r.top * sy, r.width * sx, r.height * sy, 0, 0, w, h);
  video.srcObject = null;
  return {
    canvas,
    width: w,
    height: h,
    blanks: [],
    styled: 0,
    incomplete: false,
    imagesMissing: 0,
  };
}
