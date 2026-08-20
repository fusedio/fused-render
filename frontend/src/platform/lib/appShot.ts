// One real-pixels screenshot of an app, for the export path (D396): when a
// folder without an authored preview.png is exported, this captures what the
// app actually looks like and downloadAppFile bakes it into the .fused as
// `files/preview.png`.
//
// The mechanism is TAB CAPTURE — getDisplayMedia with the current-tab hints —
// the same one the claude template's annotation shots use (annXO, D355), and
// for the same reason it beats DOM serialization here: fused apps are
// map/canvas/WebGL-heavy, and an SVG-foreignObject rasterization of those is
// reliably blank (no external fetches, no WebGL readback), which baked into
// the artifact would be a permanently wrong thumbnail. Live pixels have no
// such failure mode. The cost is the browser's share prompt, once per capture.
//
// Tab capture photographs VISIBLE pixels only, so the app must be on screen.
// Two sources, in preference order (owner call — no full-screen flash):
//
//   1. The card's own thumbnail. A card without a preview.png already renders
//      the live app in its thumb (AppPreviewCard's fallback chain), so the
//      pixels the user is looking at ARE the app — capture the tab and crop
//      to that rect. Nothing navigates, nothing flashes; the shot is thumb-
//      sized (~card width × devicePixelRatio), which is exactly the size the
//      card that will display it renders at.
//   2. A full-viewport stage (scrim + fresh iframe of the entry under
//      `_preview=1`/`_nofocus=1`), only when no usable thumb element was
//      offered or it is off-screen/too small to be worth photographing.
//
// Every failure — no getDisplayMedia, the user dismissing the prompt, the
// app failing to load, a zero-pixel frame — resolves to undefined, never a
// throw: the caller exports WITHOUT a preview, which is exactly what the
// export did before this existed.
//
// ORDER IS LOAD-BEARING: the prompt goes up FIRST, before the stage is mounted
// and before anything is awaited. `getDisplayMedia` requires transient user
// activation, which the click that reached here is the only source of and which
// Chrome expires ~5s later — so waiting for a stage iframe to load (up to 10s)
// and then settle (1.5s) before asking would spend the activation on the wait
// and lose the prompt for every app slower than a beat. Same lesson the
// annotation shots already carry: "the click that armed the mode is the user
// activation getDisplayMedia requires, where a later capture may have none"
// (templates/claude/template.html, annXOStreamGet's caller). A tab-capture
// stream is CONTINUOUS, so grabbing a frame long after the stream opened costs
// nothing — the stage is mounted and settled against a stream already running,
// and the frame we photograph is the one on screen at grab time.
import { downloadAppFile } from "./api";
import { withNoFocus } from "./frame-focus";
import { withPreviewFlag } from "./router";

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

// The captured PNG's width cap. A crop captures at devicePixelRatio — a
// 5k-wide preview.png inside every .fused is waste; card thumbs render
// ~400px wide.
const MAX_SHOT_WIDTH = 1600;

// Below this on-screen size a thumb crop would be photographing noise —
// take the stage instead.
const MIN_CROP_CSS_PX = { width: 120, height: 75 };

function shotUrl(entryHtml: string): string {
  return withNoFocus(withPreviewFlag(`/render?path=${encodeURIComponent(entryHtml)}`));
}

// The one export entry every card surface calls (the hover chip and the
// context menu): capture only when there is something to gain — a renderable
// page and no authored preview.png — then the ordinary download. A capture
// that comes back undefined (unsupported, dismissed, blank) exports plain.
//
// `captureEl` is the no-flash crop source above, and the CALLER'S CONTRACT on
// it is narrow: an element whose pixels ARE the app *right now*. Not "the box
// the app will render in" — a card thumb whose live iframe has not loaded is an
// empty grey box, and cropping that bakes the empty box into the artifact as
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

// Whether `el`'s box is fully inside the viewport and big enough that a crop
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

export async function captureAppPreview(
  entryHtml: string,
  captureEl?: Element | null,
): Promise<Blob | undefined> {
  if (!navigator.mediaDevices?.getDisplayMedia) return undefined;
  let stage: HTMLDivElement | undefined;
  let stream: MediaStream | undefined;
  try {
    // Which source we photograph is decided BEFORE the prompt and with nothing
    // awaited in between — see the ordering note in the module comment. Inside
    // the try like everything else, so this function keeps its one promise to
    // the export path: resolve to undefined, never throw.
    const thumb = cropRect(captureEl);
    // The prompt, on the click's own activation. Same current-tab hints as the
    // annotation shots: preselect this tab, no switching, no monitors (ignored,
    // not fatal, elsewhere).
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
    let rectOf: () => DOMRect;
    if (thumb) {
      // Re-read at grab time: the share prompt scrolls nothing, but cheap
      // insurance against layout shifting while it was up.
      rectOf = () => cropRect(captureEl) ?? thumb;
    } else {
      // No usable thumb on screen — the full-viewport stage: scrim + the
      // app's own page, visible because tab capture photographs the tab.
      // Mounted only now, AFTER the prompt resolved: a dismissed prompt then
      // costs no flash at all, and the scrim never sits under browser UI.
      stage = document.createElement("div");
      stage.style.cssText =
        "position:fixed;inset:0;z-index:2147483000;background:#fff;";
      const frame = document.createElement("iframe");
      frame.src = shotUrl(entryHtml);
      frame.style.cssText = "width:100%;height:100%;border:0;display:block;";
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
      const s = stage;
      rectOf = () => s.getBoundingClientRect();
    }
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
    // Crop the frame to the source rect (the captured frame is the whole tab
    // at the capture's own resolution — window chrome differences, zoom and
    // DPR all land in this one scale factor).
    const sx = video.videoWidth / window.innerWidth;
    const sy = video.videoHeight / window.innerHeight;
    const r = rectOf();
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
    stage?.remove();
  }
}
