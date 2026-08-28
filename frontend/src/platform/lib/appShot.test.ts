// The one thing about the export capture that no screenshot and no unit call
// can show, because it is a promise made across files: the crop source must
// be PAINTED pixels. (The capture is a native screen shot now — an earlier
// tab-capture version also pinned prompt ORDER here, against the click's
// transient user activation; a native shot raises no prompt, so that
// constraint and its tests are gone.)
//
// Source assertions, like ClaudeHealthStrip's in claude-health.test.ts and the
// card's in tests/test_pane_no_autofocus.py: `bun test` has no DOM, so there is
// no screen to shoot and no iframe to load — but the failure below is silent
// in the product (a thumbnail that is a grey box) and permanent in the
// artifact, so it is worth pinning where a refactor has to walk past it.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "bun:test";

const read = (...p: string[]) => readFileSync(join(import.meta.dir, ...p), "utf8");

const SHOT = read("appShot.ts");
// platform/ui, not apps/builder: the card moved there when a third app began
// drawing it (#765) and this path did not move with it, so the read threw
// ENOENT and took the whole file down with it before a single test ran.
const CARD = read("..", "ui", "AppPreviewCard.tsx");
const APPS = read("..", "..", "apps", "builder", "Apps.tsx");
const PREVIEW = read("..", "..", "apps", "explorer", "Preview.tsx");

// -- the crop source must be painted pixels ---------------------------------

// A card thumb whose live iframe has not loaded is an empty grey box, and the
// grid starts only two previews at a time (createPreviewStartQueue(2)), so that
// is the COMMON state of a card rather than a rare one. Cropping it bakes the
// empty box into the .fused as its permanent thumbnail — a valid PNG, so
// nothing server-side can catch it. cropRect only knows geometry, so each
// caller promises paintedness instead.
test("cropRect judges geometry only — paintedness is the caller's promise", () => {
  const fn = SHOT.slice(SHOT.indexOf("function cropRect("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).toContain("getBoundingClientRect()");
  // No readiness guess of its own: a rect cannot answer it, and a check here
  // that looked like one would let a caller stop making the promise.
  expect(body).not.toContain("data-capture-ready");
  expect(body).not.toContain("complete");
});

test("the card offers its thumb only once the body iframe has loaded", () => {
  // `bodyLive`, not `liveReady`: liveReady is the hover crossfade's flag and is
  // reset on every mouseenter, and the export chip is only reachable while
  // hovering — gating on it would gate on a flag the hover just cleared.
  expect(CARD).toContain("exportAppFile(app, bodyLive ? thumbRef.current : null)");
  expect(CARD).toContain("setBodyLive(true)");
  // Set on the BODY branch's load (the branch that renders when there is no
  // still), which is the only frame the capture ever crops.
  const bodyBranch = CARD.slice(CARD.indexOf(") : liveSrc && nearViewport && liveStarted ? ("));
  expect(bodyBranch).toContain("setBodyLive(true)");
});

test("the context menu finds the thumb by the card's paintedness attribute", () => {
  // Apps.tsx opens the menu and has no access to the card's state, so the
  // promise crosses as one attribute — same posture as the preview pane's
  // data-fused-annotate-target.
  expect(CARD).toContain('data-capture-ready={bodyLive ? "" : undefined}');
  expect(APPS).toContain('".app-pcard-thumb[data-capture-ready]"');
  // Never the bare selector: that is the version that crops empty boxes.
  expect(APPS).not.toContain('querySelector(".app-pcard-thumb")');
});

test("the preview header crops the SHOWN frame, which is the painted one", () => {
  // `.is-shown` rides `shown`, which the frame swap sets only once that frame
  // paints (see the data-fused-annotate-target comment beside it) — so it
  // satisfies the same contract without needing the card's attribute.
  expect(PREVIEW).toContain('document.querySelector(".preview-frame.is-shown")');
});
