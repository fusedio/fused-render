// THE APP PREVIEW at the top of the task side peek — its geometry, its height
// memory, and the question of whether there is an app to preview at all
// (.claude-design/task-side-peek/design.md, "App preview in the peek").
//
// When the peeked task's project folder is a fused-render app, the peek shows
// the app's ENTRY VIEW live above the conversation: the same `/render?path=…`
// document the app page's Overview tab frames and the Home card opens, real and
// interactive, reloading on a file change because that is what every `/render`
// page does for itself (static/runtime.js, LR-2). Nothing here builds a reload;
// framing the same document is the whole of it.
//
// CONTAINED, LIKE `object-fit: contain` (Akshil, 2026-09-16): the frame lays
// out at a VIRTUAL 1280×720 viewport and is CSS-scaled to the LARGER of the two
// fits — the peek's inner width (see `PREVIEW_INSET`) or the card's height —
// whichever is the tighter. The aspect is always 16:9; whichever axis has room
// to spare shows padding around the frame. So an app sees a desktop-sized
// window whatever the panel is doing — its own media queries and layout never
// see a 564px browser — and widening the peek makes the preview both wider and
// TALLER until the card's height becomes the limit.
//
// The pure half is here (`previewBox`, and the in-memory height below it) so
// the clamps can be proved without a layout — peek-preview.test.ts.
import { useEffect, useState } from "react";
import { getCurrentApps } from "@platform/lib/api";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { currentApps, isUnderDir, type CurrentApp } from "./current-apps-lib";

/** The virtual viewport the preview renders at, before scaling. 1280×720 is
 *  16:9 and a desktop width — see the header. */
export const PREVIEW_VW = 1280;
export const PREVIEW_VH = 720;

/** The box never gets smaller than this, however the seam is dragged: below it
 *  the preview is a letterbox rather than a view of anything. */
export const PREVIEW_MIN_H = 120;

/** Undragged, the preview takes at most 35% of the body — the conversation is
 *  what the peek is for, and a preview that opened at its full 16:9 height on a
 *  tall panel would push the transcript out of sight before it said anything. */
export const PREVIEW_CAP_FRACTION = 0.35;

/**
 * WHAT THE CHAT KEEPS, whatever the seam is dragged to: enough for the
 * composer and a line of transcript above it.
 *
 * "The composer must stay reachable" is the never-broken rule this number is
 * (design.md, App preview → Never broken). A drag that could take the panel's
 * whole height is a drag that can hide the only control in it.
 */
export const PREVIEW_CHAT_MIN = 180;

/**
 * THE AIR EITHER SIDE OF THE PREVIEW, and the reason the scale is not simply
 * `peekWidth / 1280` any more (Akshil, 2026-09-14 — design.md, Polish batch 4,
 * item 7).
 *
 * The box is inset by this on each side (`--peek-preview-inset` in
 * styles/task-peek.css) rather than welded to the panel's walls. The SCALE
 * has to know about that: padding on a scroller does not shrink what is
 * inside it, so a frame still drawn at `peekWidth / 1280` would simply push
 * 2×this much of app out past the gutter and grow a horizontal scrollbar
 * for it.
 *
 * Was 38 (one header-button width, so the app's edges lined up with the ×
 * and the ⋮ above it — Akshil, 2026-09-14); Akshil, 2026-09-16, cut to a
 * hairline — the alignment-with-the-header reasoning no longer applies, this
 * is just "keep the scroller from clipping the frame's own edge".
 *
 * ONE NUMBER IN TWO LANGUAGES, which is the caveat: CSS owns the gutter, this
 * owns the arithmetic, and neither can read the other. They are named in each
 * other's comments and pinned together by `peek-preview.test.ts`.
 */
export const PREVIEW_INSET = 1;

/** Air above and below the preview CARD inside the box — `padding-top` /
 *  `padding-bottom` of `.task-side-peek-preview` (styles/task-peek.css). The
 *  box's `height` includes it, so the card the app is drawn in is this much
 *  shorter, twice — and the virtual viewport has to be sized to the CARD, or
 *  the app is taller than its frame and scrolls under the border.
 *
 *  Was 12; Akshil, 2026-09-16, cut to a hairline, matching `PREVIEW_INSET`. */
export const PREVIEW_PAD_Y = 1;

/** One arrow press on the horizontal seam, matching the vertical one's. */
export const PREVIEW_KEY_STEP = 10;

/**
 * HOW LONG A PREVIEW IS GIVEN TO SAY ANYTHING.
 *
 * `onError` is not a promise an iframe keeps: a `/render` document that hangs,
 * or that boots into its own error page, loads "successfully" and never fires
 * one — so a skeleton with nothing behind it would sit there for the life of
 * the panel. After this the preview says so in one muted line and the
 * conversation below carries on, which is the honest failure.
 */
export const PREVIEW_LOAD_TIMEOUT_MS = 6000;

/** Where a preview is in its one-way trip from "nothing yet" to a verdict. */
export type PreviewLoad = "waiting" | "ready" | "failed";

/**
 * THE PREVIEW'S LOAD, as a machine — three states and four things that can
 * happen to it.
 *
 * Written down rather than juggled as two booleans because the interesting
 * transitions are the ones that must NOT happen: a `load` that lands after the
 * clock has already given up must not un-fail the strip (the document that
 * finally arrived is the error page the timeout was about), and a timeout that
 * fires against a preview which is already up must not fail a working app. Only
 * a NEW `src` returns to waiting.
 */
export function previewLoad(
  state: PreviewLoad,
  event: "src" | "load" | "error" | "timeout",
): PreviewLoad {
  if (event === "src") return "waiting";
  if (event === "error") return "failed";
  if (state !== "waiting") return state;
  return event === "load" ? "ready" : "failed";
}

export interface PreviewBox {
  /** The box's own height — what the reader sees. */
  height: number;
  /** `transform: scale()` on the 1280×720 frame — the CONTAIN scale, the
   *  smaller of the width fit and the height fit. */
  scale: number;
  /**
   * THE VIRTUAL VIEWPORT HEIGHT to give the frame — an UNSCALED length, the
   * vertical twin of `PREVIEW_VW`'s 1280.
   *
   * ALWAYS `PREVIEW_VH` now (Akshil, 2026-09-16): the aspect is locked on both
   * axes, so the frame is a 16:9 window and the scale is what bends to fit the
   * card. Kept in the box so the markup has one source for the frame's size.
   * The drawn footprint is `PREVIEW_VW * scale` × `frameHeight * scale`, and
   * whatever the card has beyond that is padding around it.
   */
  frameHeight: number;
}

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(value, high));
}

/**
 * The preview's geometry for one peek.
 *
 * `bodyHeight` is the space under the header — what the preview and the chat
 * divide between them. `dragged` is the reader's own height, or null for "they
 * have not said", which is the ordinary case and the one the cap governs.
 *
 * The clamps are applied in the order they matter: the floor first (a box
 * smaller than `PREVIEW_MIN_H` is not a preview), then the ceiling that keeps
 * the composer on screen. A body too short to honour both is given entirely to
 * the chat — no preview is better than a preview with nowhere to type.
 */
export function previewBox(
  peekWidth: number,
  bodyHeight: number,
  dragged: number | null,
): PreviewBox {
  // THE INNER WIDTH, not the panel's: the box is inset by `PREVIEW_INSET` on
  // each side, and the frame is drawn to fit what is left. A panel narrower
  // than its own two gutters has no preview at all, which `inner > 0` says.
  const inner = peekWidth - 2 * PREVIEW_INSET;
  const widthScale = inner > 0 ? inner / PREVIEW_VW : 0;
  // NO WIDTH, NO BOX. A panel narrower than its own two gutters (and a panel
  // that has not been laid out at all) has nothing to draw, and a box with a
  // height but a zero scale is a band of empty background where an app should
  // be. The width guard has to be its own: the height clamps below would
  // happily hand such a panel the 120px floor.
  if (!(widthScale > 0)) return { height: 0, scale: 0, frameHeight: 0 };
  // The BOX's natural height: the 16:9 card at the width fit plus the air
  // around it.
  const natural = PREVIEW_VH * widthScale + 2 * PREVIEW_PAD_Y;
  const ceiling = bodyHeight - PREVIEW_CHAT_MIN;
  if (!(ceiling > PREVIEW_MIN_H)) {
    return { height: 0, scale: widthScale, frameHeight: 0 };
  }
  const height =
    dragged === null
      ? clamp(Math.min(natural, bodyHeight * PREVIEW_CAP_FRACTION), PREVIEW_MIN_H, ceiling)
      : clamp(dragged, PREVIEW_MIN_H, ceiling);
  /**
   * CONTAIN, NOT CROP AND NOT STRETCH (Akshil, 2026-09-16).
   *
   * The frame is always a 1280×720 window. Its scale is the SMALLER of the
   * width fit and the height fit, so the whole frame is visible at 16:9 in a
   * card of any shape: a card wider than 16:9 shows padding left and right, a
   * card taller than 16:9 shows padding above and below. Dragging the seam
   * shorter shrinks the app; dragging it taller than the width fit only adds
   * air. Nothing outside the frame scrolls.
   */
  // The CARD is what the frame fits into — the box minus its vertical padding.
  const card = Math.max(0, height - 2 * PREVIEW_PAD_Y);
  const heightScale = card / PREVIEW_VH;
  const scale = Math.min(widthScale, heightScale);
  return { height, scale, frameHeight: PREVIEW_VH };
}

// ---- the dragged height, in memory only --------------------------------------
//
// NOT localStorage and not sessionStorage, deliberately (design.md): the height
// is a thing the reader did to THIS SITTING of the Tasks page — it survives
// swapping to another task, which is the point, and it is gone on a reload or
// on leaving the page, where the 35% rule takes over again. A number that came
// back a week later, over a different app on a different window, would be a
// memory of nothing.

let draggedHeight: number | null = null;
const listeners = new Set<() => void>();

export function getPreviewHeight(): number | null {
  return draggedHeight;
}

export function setPreviewHeight(height: number | null): void {
  const next = height === null ? null : Math.round(height);
  if (next === draggedHeight) return;
  draggedHeight = next;
  listeners.forEach((fn) => fn());
}

/** Leaving `/tasks` — the sitting is over (`setPeekHost(false)` spends this). */
export function resetPreviewHeight(): void {
  setPreviewHeight(null);
}

export function subscribePreviewHeight(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ---- is this project an app? -------------------------------------------------
//
// THE DESK'S OWN TABLE, and no second predicate: `/api/current-apps` is what
// Home lists, what the sidebar links to and what the app page is a page OF, so
// an app is exactly a folder that table names. Inventing a test here — a
// `pyproject.toml`, an `index.html`, a guess at an entry — would be a fifth
// answer to a question three surfaces already agree on, and it would drift.
//
// Cached at module level for the life of the page, like the chat template
// cache in TaskCards and for the same reason: the peek remounts constantly
// (every task swap, every view switch) and the desk's table does not change
// under a reader looking at a conversation.

let appsCache: CurrentApp[] | null = null;
let appsInFlight: Promise<void> | null = null;

/**
 * THE TABLE CHANGED — someone added an app, removed one, renamed a folder. The
 * desk announces it on the window (`platform/lib/tasksChanged`) and every other
 * reader of `/api/current-apps` listens; this cache would otherwise serve a
 * removed app's entry page for the rest of the visit, and would never learn
 * about one added while the reader had a task open.
 *
 * Listened for at MODULE level rather than in the hook: the cache is
 * module-level too, and a per-mount listener would forget the invalidation the
 * moment the panel closed.
 */
if (typeof window !== "undefined") {
  window.addEventListener(CURRENT_APPS_CHANGED_EVENT, () => {
    appsCache = null;
  });
}

/** Drop the cache so the next read goes to the server. Spent by a FRESH peek
 *  open (the store's `openPeek` from closed): the panel is about to frame an
 *  app for minutes, and a table read once at page load is the one input it
 *  cannot afford to be wrong about. An open that merely SWAPS task does not —
 *  that is one table read per keystroke of ⌃⇧J. */
export function forgetAppsCache(): void {
  appsCache = null;
}

export function ensureApps(): Promise<void> {
  // THE CACHE CHECK BELONGS HERE, not in the caller. It lived in the hook, and
  // every other caller — the store's fresh-open re-read, a second surface —
  // would have gone to the network over a table already in memory.
  if (appsCache) return Promise.resolve();
  if (appsInFlight) return appsInFlight;
  appsInFlight = getCurrentApps()
    .then(({ apps }) => {
      appsCache = currentApps(apps, []);
    })
    .catch(() => {
      // No desk, no preview. A failed read is "there is no app here" rather
      // than an error the peek shows: the conversation is what the panel is
      // for, and it is unaffected.
      appsCache = [];
    })
    .finally(() => {
      appsInFlight = null;
    });
  return appsInFlight;
}

/** The table as it stands, or null when it has not been read. For tests and for
 *  the hook's first render; everything else asks `useAppForProject`. */
export function knownApps(): CurrentApp[] | null {
  return appsCache;
}

/** The app whose folder holds `project`, or null. Pure — the table is given. */
export function appForProject(
  project: string,
  apps: readonly CurrentApp[] | null,
): CurrentApp | null {
  if (!project || !apps) return null;
  // The DEEPEST match wins: an app nested inside another app's folder is that
  // task's app, not its ancestor. Longest path is deepest by construction.
  let best: CurrentApp | null = null;
  for (const app of apps) {
    if (!app.exists || !app.entry) continue;
    if (!isUnderDir(project, app.path)) continue;
    if (!best || app.path.length > best.path.length) best = app;
  }
  return best;
}

/** The app the peeked task belongs to, or null — null while the desk's table is
 *  still being read, which reads as "no preview" and simply becomes one when
 *  the answer lands. */
export function useAppForProject(project: string): CurrentApp | null {
  const [apps, setApps] = useState<CurrentApp[] | null>(appsCache);
  // `project` is in the deps so a task swap re-checks a cache that may have
  // been dropped since — and so a mount that arrives while a read is already in
  // flight waits on that ONE promise rather than starting a second (`loadApps`
  // returns the in-flight one).
  useEffect(() => {
    if (appsCache) {
      setApps(appsCache);
      return;
    }
    let live = true;
    void ensureApps().then(() => {
      if (live) setApps(appsCache);
    });
    return () => {
      live = false;
    };
  }, [project]);
  return appForProject(project, apps);
}

/** Test seam — the cache and the height are module state. */
export function resetPeekPreviewForTests(): void {
  appsCache = null;
  appsInFlight = null;
  draggedHeight = null;
  listeners.clear();
}
