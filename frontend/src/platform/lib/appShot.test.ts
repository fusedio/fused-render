// The two things about the export capture that no screenshot and no unit call
// can show, because both are about ORDER and about a promise made across files.
//
// Source assertions, like ClaudeHealthStrip's in claude-health.test.ts and the
// card's in tests/test_pane_no_autofocus.py: `bun test` has no DOM, so there is
// no getDisplayMedia to stub and no iframe to load — but both failures below
// are silent in the product (a share prompt that never appears; a thumbnail
// that is a grey box) and permanent in the artifact, so they are worth pinning
// where a refactor has to walk past them.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "bun:test";

const read = (...p: string[]) => readFileSync(join(import.meta.dir, ...p), "utf8");

const SHOT = read("appShot.ts");
const CARD = read("..", "..", "apps", "builder", "AppPreviewCard.tsx");
const APPS = read("..", "..", "apps", "builder", "Apps.tsx");
const PREVIEW = read("..", "..", "apps", "explorer", "Preview.tsx");

// -- the prompt rides the click's activation ---------------------------------

// getDisplayMedia needs transient user activation, which Chrome expires a few
// seconds after the click. The stage waits for an iframe load (up to 10s) and
// then settles (1.5s), so asking for the stream after that wait loses the
// prompt for every app slower than a beat — silently, since a dismissed or
// refused prompt and an expired one are the same rejection and the same
// export-plain fallback. The stream is continuous, so the fix costs nothing:
// ask first, mount the stage against a stream already running.
test("getDisplayMedia is called before the stage is mounted or awaited", () => {
  const prompt = SHOT.indexOf("getDisplayMedia({");
  const mount = SHOT.indexOf("document.body.appendChild(stage)");
  const settle = SHOT.indexOf("setTimeout(res, SETTLE_MS)");
  expect(prompt).toBeGreaterThan(-1);
  expect(mount).toBeGreaterThan(-1);
  expect(settle).toBeGreaterThan(-1);
  expect(prompt).toBeLessThan(mount);
  expect(prompt).toBeLessThan(settle);
});

test("nothing is awaited between entering the capture and the prompt", () => {
  // The body from the support check up to (not including) the prompt's own
  // statement: a single `await` in there is the whole bug, whatever it awaits.
  const start = SHOT.indexOf(
    "if (!navigator.mediaDevices?.getDisplayMedia) return undefined;",
  );
  const promptStmt = SHOT.indexOf("stream = await navigator.mediaDevices");
  expect(start).toBeGreaterThan(-1);
  expect(promptStmt).toBeGreaterThan(start);
  // Comments stripped: the prose in here says "awaited", and the assertion is
  // about code.
  const code = SHOT.slice(start, promptStmt)
    .split("\n")
    .filter((l) => !l.trim().startsWith("//"))
    .join("\n");
  expect(code).not.toContain("await");
});

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
