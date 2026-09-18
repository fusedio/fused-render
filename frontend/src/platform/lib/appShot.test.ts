// The promises around the app preview capture that no unit call can show,
// because they are made across files. `bun test` has no DOM, so there is no
// screen to shoot and no iframe to load — source assertions, like
// ClaudeHealthStrip's in claude-health.test.ts, pin what a refactor has to
// walk past.
//
// The capture is EXPLICIT ONLY: the explorer's "Set Current View as Preview".
// Share used to shoot implicitly for a folder without a preview.png (D396)
// and that is retired — appShot.ts's header has the why. The failures pinned
// here are silent in the product and permanent in the artifact.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "bun:test";

const read = (...p: string[]) => readFileSync(join(import.meta.dir, ...p), "utf8");

const SHOT = read("appShot.ts");
const SHARE = read("share-app.ts");
const MODAL = read("..", "ui", "ShareAppModal.tsx");
const PREVIEW = read("..", "..", "apps", "explorer", "Preview.tsx");

// -- share never photographs the screen -------------------------------------

// A screen shot at share time depended on the Screen Recording grant, the
// window sitting on one display, zoom at 100% and overlay UI hiding in time;
// each failed at least once and baked a wrong picture into a .fused or a
// public link's landing page. App Doctor's `preview` check owns the missing
// thumbnail now. Neither share route may reach for the capture again.
test("neither share route captures a preview", () => {
  expect(SHARE).not.toContain("captureAppPreview");
  expect(SHARE).not.toContain("captureEl");
  expect(MODAL).not.toContain("captureAppPreview");
  expect(MODAL).not.toContain('from "@platform/lib/appShot"');
  // The file route is the plain disk write — with the caller's own display
  // name, which AppPage.tsx and EntryActionsMenu.tsx version-suffix so a v7
  // export never collides with a live export in Downloads.
  expect(MODAL).toContain("saveAppFileToDisk(file.path, file.name)");
  // publishShare sends the path alone; the server reads the folder's still.
  const publish = SHARE.slice(SHARE.indexOf("export async function publishShare("));
  expect(publish.slice(0, publish.indexOf("\n}"))).not.toContain("preview");
});

// -- the crop source must be painted pixels ---------------------------------

// cropRect only knows geometry; whether the element shows the app is the
// caller's promise. No readiness guess of its own: a check here that looked
// like one would let a caller stop making the promise.
test("cropRect judges geometry only — paintedness is the caller's promise", () => {
  const fn = SHOT.slice(SHOT.indexOf("function cropRect("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).toContain("getBoundingClientRect()");
  expect(body).not.toContain("complete");
});

test("captureAppPreview photographs the offered element or nothing — no stage", () => {
  const fn = SHOT.slice(SHOT.indexOf("export async function captureAppPreview("));
  const body = fn.slice(0, fn.indexOf("\n}"));
  expect(body).toContain("cropRect(captureEl)");
  expect(body).not.toContain("createElement(\"iframe\")");
  expect(body).not.toContain("appendChild");
});

test("the preview header crops the SHOWN frame, which is the painted one", () => {
  // `.is-shown` rides `shown`, which the frame swap sets only once that frame
  // paints (see the data-fused-annotate-target comment beside it).
  expect(PREVIEW).toContain('document.querySelector(".preview-frame.is-shown")');
});
