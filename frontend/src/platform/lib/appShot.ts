// One real-pixels screenshot of an app's entry page, for the export path
// (D392): when a folder without an authored preview.png is exported, this
// captures what the app actually looks like and downloadAppFile bakes it into
// the .fused as `files/preview.png`.
//
// The mechanism is TAB CAPTURE — getDisplayMedia with the current-tab hints —
// the same one the claude template's annotation shots use (annXO, D355), and
// for the same reason it beats DOM serialization here: fused apps are
// map/canvas/WebGL-heavy, and an SVG-foreignObject rasterization of those is
// reliably blank (no external fetches, no WebGL readback), which baked into
// the artifact would be a permanently wrong thumbnail. Live pixels have no
// such failure mode. The cost is honest and visible: the browser shows its
// share prompt once per capture, and the app is rendered full-viewport for
// the moment the frame is grabbed.
//
// The overlay must be VISIBLE — tab capture photographs the tab, so a hidden
// or offscreen iframe would photograph its absence. The app loads under
// `_preview=1` (no open recorded, D301) and `_nofocus=1` (no keyboard theft,
// D348), a scrim behind it so whatever the grid shows doesn't bleed through
// the app's own transparent regions.
//
// Every failure — no getDisplayMedia, the user dismissing the prompt, the
// app failing to load, a zero-pixel frame — resolves to undefined, never a
// throw: the caller exports WITHOUT a preview, which is exactly what the
// export did before this existed.
import { downloadAppFile, type AppInfo } from "./api";
import { withNoFocus } from "./frame-focus";
import { withPreviewFlag } from "./router";

// The one export entry every card surface calls (the hover chip and the
// context menu): capture only when there is something to gain — a renderable
// page and no authored preview.png — then the ordinary download. A capture
// that comes back undefined (unsupported, dismissed, blank) exports plain.
export async function exportAppFile(app: AppInfo): Promise<void> {
  const preview =
    !app.preview_image && app.entry_html
      ? await captureAppPreview(app.entry_html)
      : undefined;
  return downloadAppFile(app.path, app.name, preview);
}

// How long the app gets to paint something worth photographing after its
// frame's load event: boot scripts, first fetch, first map tiles. A capture
// is a one-off user action, so erring generous beats a blank shot.
const SETTLE_MS = 1500;

// The captured PNG's width cap. The crop is the viewport, which on a retina
// display captures at devicePixelRatio — a 5k-wide preview.png inside every
// .fused is waste; card thumbs render ~400px wide.
const MAX_SHOT_WIDTH = 1600;

function shotUrl(entryHtml: string): string {
  return withNoFocus(withPreviewFlag(`/render?path=${encodeURIComponent(entryHtml)}`));
}

export async function captureAppPreview(entryHtml: string): Promise<Blob | undefined> {
  if (!navigator.mediaDevices?.getDisplayMedia) return undefined;
  // The full-viewport stage: scrim + the app's own page.
  const stage = document.createElement("div");
  stage.style.cssText =
    "position:fixed;inset:0;z-index:2147483000;background:#fff;";
  const frame = document.createElement("iframe");
  frame.src = shotUrl(entryHtml);
  frame.style.cssText = "width:100%;height:100%;border:0;display:block;";
  frame.tabIndex = -1;
  stage.appendChild(frame);
  document.body.appendChild(stage);
  let stream: MediaStream | undefined;
  try {
    const loaded = new Promise<void>((res) => {
      // load OR error: an app that never loads still resolves — the blank
      // scrim capture is discarded by nothing, but the export must not hang.
      frame.addEventListener("load", () => res(), { once: true });
      frame.addEventListener("error", () => res(), { once: true });
      setTimeout(res, 10_000);
    });
    await loaded;
    await new Promise((res) => setTimeout(res, SETTLE_MS));
    // The prompt. Same current-tab hints as the annotation shots: preselect
    // this tab, no switching, no monitors (ignored, not fatal, elsewhere).
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "browser" },
      audio: false,
      // Chromium-only hints, absent from the lib.dom types — same cast the
      // claude template's capture relies on.
      ...({
        preferCurrentTab: true,
        selfBrowserSurface: "include",
        surfaceSwitching: "exclude",
        monitorTypeSurfaces: "exclude",
      } as object),
    });
    const video = document.createElement("video");
    video.muted = true;
    video.srcObject = stream;
    await video.play();
    // One PAINTED frame — play() resolving does not mean pixels arrived.
    if ("requestVideoFrameCallback" in video) {
      await new Promise((res) =>
        (video as HTMLVideoElement & {
          requestVideoFrameCallback: (cb: () => void) => void;
        }).requestVideoFrameCallback(() => res(undefined)),
      );
    } else {
      await new Promise((res) => setTimeout(res, 150));
    }
    if (!video.videoWidth || !video.videoHeight) return undefined;
    // Crop the frame to the stage's rect (the captured frame is the whole
    // tab at the capture's own resolution — window chrome differences, zoom
    // and DPR all land in this one scale factor).
    const sx = video.videoWidth / window.innerWidth;
    const sy = video.videoHeight / window.innerHeight;
    const r = stage.getBoundingClientRect();
    const srcW = Math.max(1, r.width * sx);
    const srcH = Math.max(1, r.height * sy);
    const scale = Math.min(1, MAX_SHOT_WIDTH / srcW);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(srcW * scale));
    canvas.height = Math.max(1, Math.round(srcH * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return undefined;
    ctx.drawImage(video, r.left * sx, r.top * sy, srcW, srcH,
      0, 0, canvas.width, canvas.height);
    video.srcObject = null;
    return await new Promise<Blob | undefined>((res) =>
      canvas.toBlob((b) => res(b ?? undefined), "image/png"),
    );
  } catch {
    // Prompt dismissed, capture unsupported, play() refused — all the same
    // outcome: export without a preview.
    return undefined;
  } finally {
    stream?.getTracks().forEach((t) => t.stop());
    stage.remove();
  }
}
