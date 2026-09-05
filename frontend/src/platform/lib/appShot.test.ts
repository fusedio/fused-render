// The things about the export/preview capture that no screenshot and no unit
// call can show, because they are promises made across files.
//
// The capture is a DOM CLONE now (D635) — the owner reverted the native screen
// shot (`POST /api/capture/shot-region`, #889). Two whole classes of constraint
// went with it and their tests are gone: the click's transient user activation
// (a share prompt, from the even earlier tab-capture version) and the
// screen-rect mapping (viewport origin, on-screen containment, one-display
// windows). What is LEFT to pin is the contract that survived every mechanism
// change — the capture source must be a frame whose document IS the app — plus
// the negative facts that keep the reverted mechanism from creeping back.
//
// Source assertions, like ClaudeHealthStrip's in claude-health.test.ts and the
// card's in tests/test_pane_no_autofocus.py: `bun test` has no DOM, so there is
// no document to clone and no iframe to load — but the failure below is silent
// in the product (a thumbnail that is a grey box) and permanent in the
// artifact, so it is worth pinning where a refactor has to walk past it.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "bun:test";

const read = (...p: string[]) => readFileSync(join(import.meta.dir, ...p), "utf8");

const SHOT = read("appShot.ts");
// The module's CODE, without the header. The negative assertions below have to
// read this and not `SHOT`: the header's whole job is to name the mechanism it
// replaced, so it says "shot-region" and "pointerdown" on purpose, and a search
// over the whole file would fail on the documentation rather than on a
// regression.
const CODE = SHOT.slice(SHOT.indexOf('import { downloadAppFile }'));
// platform/ui, not apps/builder: the card moved there when a third app began
// drawing it (#765) and this path did not move with it, so the read threw
// ENOENT and took the whole file down with it before a single test ran.
const CARD = read("..", "ui", "AppPreviewCard.tsx");
const APPS = read("..", "..", "apps", "builder", "Apps.tsx");
const PREVIEW = read("..", "..", "apps", "explorer", "Preview.tsx");

// -- the capture source must be a rendered document -------------------------

// A card thumb whose live iframe has not loaded has an EMPTY body, and the grid
// starts only two previews at a time (createPreviewStartQueue(2)), so that is
// the COMMON state of a card rather than a rare one. Cloning it bakes an empty
// box into the .fused as its permanent thumbnail — a valid PNG, so nothing
// server-side can catch it. cropRect only knows geometry, so each caller
// promises renderedness instead.
test("cropRect judges geometry only — renderedness is the caller's promise", () => {
  const fn = SHOT.slice(SHOT.indexOf("export function cropRect("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).toContain("getBoundingClientRect()");
  // No readiness guess of its own: a rect cannot answer it, and a check here
  // that looked like one would let a caller stop making the promise.
  expect(body).not.toContain("data-capture-ready");
  expect(body).not.toContain("complete");
});

// The containment test existed ONLY because a screen shot photographs visible
// pixels. A clone reads the frame's document, so a half-scrolled-out card
// clones exactly as well as a centred one — and keeping the test would refuse
// captures that now work, which is the "has to be fully on screen" error the
// preview header used to be able to show.
test("cropRect no longer requires the frame to be inside the viewport", () => {
  const fn = SHOT.slice(SHOT.indexOf("export function cropRect("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).not.toContain("window.innerWidth");
  expect(body).not.toContain("window.innerHeight");
  expect(body).not.toContain("r.left < 0");
});

test("the card offers its thumb only once the body iframe has loaded", () => {
  // `bodyLive`, not `liveReady`: liveReady is the hover crossfade's flag and is
  // reset on every mouseenter, and the export chip is only reachable while
  // hovering — gating on it would gate on a flag the hover just cleared.
  expect(CARD).toContain("exportAppFile(app, bodyLive ? thumbRef.current : null)");
  expect(CARD).toContain("setBodyLive(true)");
  // Set on the BODY branch's load (the branch that renders when there is no
  // still), which is the only frame the capture ever clones.
  const bodyBranch = CARD.slice(CARD.indexOf(") : liveSrc && nearViewport && liveStarted ? ("));
  expect(bodyBranch).toContain("setBodyLive(true)");
});

test("the context menu finds the thumb by the card's paintedness attribute", () => {
  // Apps.tsx opens the menu and has no access to the card's state, so the
  // promise crosses as one attribute — same posture as the preview pane's
  // data-fused-annotate-target.
  expect(CARD).toContain('data-capture-ready={bodyLive ? "" : undefined}');
  expect(APPS).toContain('".app-pcard-thumb[data-capture-ready]"');
  // Never the bare selector: that is the version that captures empty boxes.
  expect(APPS).not.toContain('querySelector(".app-pcard-thumb")');
});

test("the preview header captures the SHOWN frame, which is the rendered one", () => {
  // `.is-shown` rides `shown`, which the frame swap sets only once that frame
  // paints (see the data-fused-annotate-target comment beside it) — so it
  // satisfies the same contract without needing the card's attribute.
  expect(PREVIEW).toContain('document.querySelector(".preview-frame.is-shown")');
});

// -- the clone, and the native shot staying gone -----------------------------

// The whole point of D635. A stray `shot-region` call would put the OS
// permission dialog, the on-screen requirement and the side-panel geometry bug
// straight back.
test("nothing in the capture path calls the native screen-shot route", () => {
  expect(CODE).not.toContain("shot-region");
  expect(CODE).not.toContain("/api/capture/");
  // The screen-rect mapping and everything that fed it.
  expect(CODE).not.toContain("screenRect");
  expect(CODE).not.toContain("screenX");
  // And no fetch at all: the clone is entirely client-side.
  expect(CODE).not.toContain("fetch(");
});

// This listener was module-level, so merely IMPORTING appShot.ts threw in any
// test whose `window` stub had no addEventListener — appCardMenu.test.ts failed
// in isolation on main for exactly that reason. It existed only to learn the
// viewport's screen origin for the native shot, so the clone deletes both.
test("no module-load window listener — the viewport origin is not needed", () => {
  expect(CODE).not.toContain("viewportOrigin");
  expect(CODE).not.toContain("pointerdown");
  expect(CODE).not.toContain("window.addEventListener");
});

test("the capture clones the frame's own same-origin document", () => {
  expect(SHOT).toContain('from "./domShot"');
  expect(SHOT).toContain("domShot(win,");
  // Cross-origin is the one case a clone cannot serve, and it is detected by a
  // null contentDocument rather than by catching a throw.
  expect(SHOT).toContain("contentDocument");
  expect(SHOT).toContain("HTMLIFrameElement");
});

test("the clone is drawn at the frame's LAYOUT size x DPR, capped", () => {
  const fn = SHOT.slice(SHOT.indexOf("async function shootFrame("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).toContain("devicePixelRatio");
  // Math.min, not a bare dpr: at 2x a wide preview pane would otherwise be
  // drawn at 4k and thrown away again by capWidth.
  expect(body).toContain("Math.min(dpr, MAX_SHOT_WIDTH");
  // The layout box, NOT getBoundingClientRect: a card thumb lays its frame out
  // at 1280×800 behind scale(0.25), and a clone cut to the visual rect would be
  // the page's top-left quarter (Bugbot on #919). cropRect still gates above.
  expect(body).toContain("frame.offsetWidth || r.width");
  expect(body).toContain("frame.offsetHeight || r.height");
  expect(body).not.toContain("width: r.width");
});

// Every failure ships the export WITHOUT a preview rather than throwing — the
// behaviour the export had before any capture existed.
test("captureAppPreview resolves undefined for every failure", () => {
  const fn = SHOT.slice(SHOT.indexOf("export async function captureAppPreview("));
  expect(fn).toContain("return undefined");
  expect(fn).toContain("} catch {");
  // The stage is still torn down and the attribute still removed on every path.
  expect(fn).toContain("stage?.remove()");
  expect(fn).toContain("removeAttribute(SHOOTING_ATTR)");
});

// The header has to say what the mechanism is and what it costs: a WebGL pane
// rasterises blank, and fused apps are map-heavy, so a reader deciding whether
// to trust a baked thumbnail needs that stated rather than discovered.
test("the module header states the decision and the WebGL cost plainly", () => {
  const header = SHOT.slice(0, SHOT.indexOf("import "));
  expect(header).toContain("DOM CLONE");
  expect(header).toContain("D635");
  expect(header).toMatch(/RASTERISES BLANK|rasterises blank/);
  expect(header).toContain("preserveDrawingBuffer");
});

// Bugbot on #919: the card export hands over the `.app-pcard-thumb` SPAN, whose
// iframe is a child. Judging the span itself let it pass cropRect, skipped the
// stage, and then found no frame to clone — a .fused with no preview and no
// error. The frame is resolved out of whatever was offered, once, and every
// later step (geometry, window, clone) runs on that frame.
test("a wrapper source resolves to the iframe inside it before anything is judged", () => {
  expect(SHOT).toContain("export function frameOf(el: Element): HTMLIFrameElement | null {");
  expect(SHOT).toContain('return el.querySelector("iframe");');
  const pick = SHOT.slice(SHOT.indexOf("let source: Element;"),
                          SHOT.indexOf("} else if (opts.stage === false) {"));
  expect(pick).toContain("const live = captureEl ? frameOf(captureEl) : null;");
  expect(pick).toContain("if (live && cropRect(live)) {");
  expect(pick).not.toContain("cropRect(captureEl)");
  const shoot = SHOT.slice(SHOT.indexOf("async function shootFrame("));
  expect(shoot).toContain("const frame = frameOf(source);");
  expect(shoot).toContain("const r = cropRect(frame);");
});
