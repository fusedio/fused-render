// ONE PICTURE OF THE PANE (T:9958 shotPane, 10088 shotCapturePane, 10127
// annCaptureOverview, 10199 annOverview, 11232 shotFlash).
//
// The order is T's exactly, and it is a preference rather than a gate: NATIVE
// screen shot first (live pixels, no prompt, WebGL included), then the TAB share
// for a cross-origin target that could not be shot that way, then the DOM clone
// for a readable one. Nothing that worked stops working.
//
// Bounded by SHOT_TIMEOUT_MS, and the timer is only HALF of what makes the budget
// real: a `setTimeout` cannot fire while synchronous code runs, so the deadline
// handed DOWN to the style walk is the other half (T:10199). A timed-out capture
// is abandoned, not cancelled — so it is still watched to the end, because it may
// still reject (an unhandled rejection in the console) and may still have minted
// an object URL nobody holds a handle to any more.
import { APP_STATE_UNREADABLE } from "../pane/paneUrl";
import { captureDom, caveatsOf } from "./dom-capture";
import { encode, encodeBadged } from "./encode";
import { captureNative } from "./native-capture";
import { captureXO } from "./xo-capture";
import {
  SHOT_FLASH_MS,
  SHOT_TIMEOUT_MS,
  SHOT_VIEW_BYTES,
  SHOT_VIEW_EDGE,
  type CaptureResult,
  type PaneBitmap,
  type ShotBadge,
} from "./types";

/** Injected in tests, and the seam PR3 uses to hand in its own overlay set and
 *  stage-rect function. */
export interface CaptureStrategies {
  native?: (frame: HTMLIFrameElement | null, deadline: number) => Promise<PaneBitmap | null>;
  tab?: (frame: HTMLIFrameElement | null) => Promise<PaneBitmap | null>;
  dom?: (win: Window, deadline: number) => Promise<PaneBitmap | null>;
}

export interface CaptureOptions {
  /** True when the pane's document is not ours to read (D349) — the only case the
   *  tab share is tried in (T:9963). */
  xo?: boolean;
  /** The app's own window for the DOM path; defaults to the frame's
   *  `contentWindow`, guarded, since the pane is `/render?path=…` on our own
   *  origin (D3/D4). */
  appWindow?: () => Window | null;
  /** Element rect → pane-bitmap space, for the blank-region caveat. PR3 passes
   *  `annStageRect`. */
  rectOf?: (el: Element) => { left: number; top: number; width: number; height: number };
  strategies?: CaptureStrategies;
  now?: () => number;
  timeoutMs?: number;
}

/**
 * IS THE PANE'S DOCUMENT OURS TO READ? T's `annXO`, and the same test verbatim:
 * a frame is there and its `contentDocument` cannot be reached — either the
 * getter throws or it answers null (T:6123-6129 `annTargetDoc`, T:6567).
 *
 * The ONE thing the tab share is tried for (T:9963). `capture` used to ask
 * `attachPane` for a picture with no options at all, so `xo` was never true and
 * a cross-origin pane that the native path could not shoot fell through to a DOM
 * clone of a document this page cannot open — which is no picture at all
 * (Bugbot, PR #1064).
 *
 * A same-origin frame mid-navigation reads cross-origin for a beat and simply
 * resolves on the next gesture, exactly as it does in T: the cost of being wrong
 * that way is one tab-share prompt, where the cost of the opposite is a capture
 * that can only fail.
 */
export function frameIsCrossOrigin(frame: HTMLIFrameElement | null): boolean {
  if (!frame) return false;
  try {
    return !frame.contentDocument;
  } catch {
    return true; // cross-origin: the getter itself is refused
  }
}

function appWindowOf(frame: HTMLIFrameElement | null): Window | null {
  try {
    if (!frame || !frame.isConnected) return null;
    const win = frame.contentWindow;
    if (!win || !win.document) return null;
    if (String(win.location.href) === "about:blank") return null;
    return win;
  } catch {
    return null;
  }
}

/** T's `shotPane`: the three paths, in order, first non-null wins (T:9958). */
export async function capturePaneBitmap(
  frame: HTMLIFrameElement | null,
  deadline: number,
  opts: CaptureOptions = {},
): Promise<{ pane: PaneBitmap | null; via: CaptureResult["via"] }> {
  const s = opts.strategies || {};
  const native = await (s.native || ((f, d) => captureNative(f, d)))(frame, deadline);
  if (native) return { pane: native, via: "native" };
  if (opts.xo) {
    const tab = await (s.tab || ((f) => captureXO(f)))(frame);
    return tab ? { pane: tab, via: "tab" } : { pane: null, via: "none" };
  }
  const win = (opts.appWindow || (() => appWindowOf(frame)))();
  if (!win) return { pane: null, via: "none" };
  const dom = await (s.dom || ((w, d) => captureDom(w, d)))(win, deadline);
  return dom ? { pane: dom, via: "dom" } : { pane: null, via: "none" };
}

/** ONE picture of the WHOLE visible pane, encoded but NOT uploaded — the upload
 *  and the naming belong to `attach.ts`, which is the only place that knows the
 *  shots dir (T:10088).
 *
 *  Never throws for a reason the user could act on: every "cannot" comes back as
 *  `blob:null` plus the `why` sentence the chip and the wire's `viewNote` carry.
 *  A failed capture still becomes a chip and still rides the message — degrading
 *  to "no image" is right, degrading to "no evidence anything was asked for" is
 *  the silent-failure shape (T:11305). */
export function capturePane(
  frame: HTMLIFrameElement | null,
  opts: CaptureOptions = {},
): Promise<CaptureResult> {
  return race((deadline) => paneShot(frame, deadline, opts), "no pane screenshot", opts);
}

/** The send-time overview: the same picture with a red letter badge burned in at
 *  each note's spot, so the model reconciles words with pixels through ONE
 *  picture instead of N (T:10127).
 *
 *  The badge POINTS are resolved by the annotation subsystem (PR3's
 *  `annPointXY` / `iu,iv` content-box fractions / element centres, T:10143-10171)
 *  and handed in here already in pane-bitmap space. */
export function captureOverview(
  frame: HTMLIFrameElement | null,
  badges: ShotBadge[],
  opts: CaptureOptions = {},
): Promise<CaptureResult> {
  return race(
    (deadline) => paneShot(frame, deadline, opts, badges),
    "no overview screenshot",
    opts,
    "overview screenshot skipped:",
  );
}

async function paneShot(
  frame: HTMLIFrameElement | null,
  deadline: number,
  opts: CaptureOptions,
  badges?: ShotBadge[],
): Promise<CaptureResult> {
  const { pane, via } = await capturePaneBitmap(frame, deadline, opts);
  if (!pane) {
    return {
      blob: null,
      width: 0,
      height: 0,
      notes: [],
      via: "none",
      incomplete: false,
      why: APP_STATE_UNREADABLE,
    };
  }
  const limits = { maxEdge: SHOT_VIEW_EDGE, maxBytes: SHOT_VIEW_BYTES };
  const blob = badges
    ? await encodeBadged(pane, badges, limits)
    : await encode(pane, { left: 0, top: 0, width: pane.width, height: pane.height }, limits);
  if (!blob) {
    return {
      blob: null,
      width: pane.width,
      height: pane.height,
      notes: [],
      via,
      incomplete: pane.incomplete,
      why: "it did not fit the " + SHOT_VIEW_BYTES + "-byte budget",
    };
  }
  return {
    blob,
    width: pane.width,
    height: pane.height,
    // Never suppressed for a blank canvas, only annotated: suppressing the pane
    // leaves nothing at all, so the caveat rides beside the real picture
    // (T:10104).
    notes: caveatsOf(pane, opts.rectOf),
    via,
    incomplete: pane.incomplete,
    thumb: URL.createObjectURL(blob),
  };
}

/** The budget, and the abandoned capture's own aftercare (T:10199, 11278).
 *
 *  `warnAs` is the OVERVIEW road's and only its: a throw there is not news for
 *  the wire — the message is what matters and the picture is an aid — so T
 *  console.warns it under one phrase and puts the ABANDONED sentence on the
 *  wire, the same one the timeout writes (T:10240-10248). The pane road has no
 *  `warnAs` and keeps "the capture failed (…)", because there the user pressed a
 *  button and is standing in front of the answer (T:11291). */
function race(
  run: (deadline: number) => Promise<CaptureResult>,
  noun: string,
  opts: CaptureOptions,
  warnAs?: string,
): Promise<CaptureResult> {
  const now = opts.now ?? (() => Date.now());
  const ms = opts.timeoutMs ?? SHOT_TIMEOUT_MS;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let abandoned = false;
  const budget = new Promise<null>((res) => {
    timer = setTimeout(() => {
      abandoned = true;
      res(null);
    }, ms);
  });
  const capture = run(now() + ms);
  // Attached to the capture ITSELF, not to the race: the race stops listening the
  // moment the timeout wins, and everything after that point belongs to nobody —
  // a late rejection is an unhandled promise rejection in the console, and a late
  // thumbnail is a Blob pinned for the life of the page with no handle left.
  capture.then(
    (late) => {
      if (abandoned && late && late.thumb) {
        try {
          URL.revokeObjectURL(late.thumb);
        } catch {
          /* already gone */
        }
      }
    },
    (err: unknown) => {
      if (abandoned) console.warn("abandoned capture failed:", err);
    },
  );
  return Promise.race([capture, budget])
    .catch((err: unknown) => {
      const why = err instanceof Error ? err.message : String(err);
      // T:10240 — the overview's throw is a console line, and the wire gets the
      // sentence the timeout would have written (the `null` below).
      if (warnAs) {
        console.warn(warnAs, why);
        return null;
      }
      const failed: CaptureResult = {
        blob: null,
        width: 0,
        height: 0,
        notes: [],
        via: "none",
        incomplete: false,
        why: "the capture failed (" + why + ")",
      };
      return failed;
    })
    .then((r) => {
      if (timer) clearTimeout(timer);
      if (r) return r;
      return {
        blob: null,
        width: 0,
        height: 0,
        notes: [],
        via: "none",
        incomplete: false,
        why: "the capture did not finish within " + ms + "ms and was abandoned",
      } satisfies CaptureResult;
    })
    .then((r) => (r.why ? { ...r, why: noun + ": " + r.why } : r));
}

/** THE SHUTTER (T:11205-11256). A white sheet over the photographed pane, and it
 *  is not decoration: the first version of this control did its whole job in
 *  silence and the report was "not obvious that it took a screenshot".
 *
 *  It flashes THE PANE, not the button, because the pane is the subject. Styled
 *  INLINE and animated with the Web Animations API rather than from a stylesheet
 *  — a rule of ours in a document of theirs is one selector away from being
 *  overruled, and this element exists for 340ms. Opacity only, no transform:
 *  that is the reduced-motion-safe form of a flash, so the signal survives
 *  `prefers-reduced-motion` instead of being switched off with the movement.
 *
 *  `[data-shot-flash]` is how the native path finds it to hide: a screen shot
 *  reads the SCREEN, and the flash is on the screen (T:11241).
 *
 *  Returns the cleanup, which is idempotent — the animation's own `finish`
 *  already runs it. */
export function flash(host: Element | null): () => void {
  const doc = host && host.ownerDocument;
  if (!host || !doc) return () => {};
  const el = doc.createElement("div");
  el.setAttribute(
    "style",
    "position: absolute; inset: 0; background: #ffffff;" +
      " opacity: 0; pointer-events: none; z-index: 2147483647;",
  );
  el.setAttribute("data-shot-flash", "");
  host.appendChild(el);
  const done = (): void => {
    try {
      el.remove();
    } catch {
      /* gone with the doc */
    }
  };
  // No `animate()` (an old engine, a detached document) is a missed flash, never
  // a failed capture (T:11248).
  if (!el.animate) {
    done();
    return done;
  }
  // Up fast, HELD briefly, then out. The hold is the part that matters: a
  // symmetric triangle peaked for one frame measured as a flicker people miss —
  // and being missed is the entire failure this exists to fix (T:11250).
  const anim = el.animate(
    [
      { opacity: 0, offset: 0 },
      { opacity: 0.45, offset: 0.18 },
      { opacity: 0.45, offset: 0.45 },
      { opacity: 0, offset: 1 },
    ],
    { duration: SHOT_FLASH_MS, easing: "ease-out" },
  );
  anim.onfinish = done;
  anim.oncancel = done;
  return done;
}
