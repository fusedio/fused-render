// The dirty guard's VISIBLE half (Modal.tsx).
//
// The guard itself — first close attempt arms, a second within ~2s discards —
// has been there since the chassis was written. What was missing is that the
// first press LOOKED like nothing happened: the only feedback was a 12px muted
// line in the footer, at the opposite corner from the ✕ the user had just
// clicked, gone again after two seconds. A tester pressed it and reported a
// dead click (QA, 2026-08-18).
//
// So the armed state is now on the button too, and these tests hold the three
// pieces together — the markup that sets the class, the stylesheet that gives it
// a look, and the wording. There is no DOM renderer in this suite (nothing here
// mounts React), so this reads the source, which is the same thing
// `new-task-form.test.ts` does for the `ready` expression.
import { beforeAll, describe, expect, test } from "bun:test";

let modal: string;
let css: string;

beforeAll(async () => {
  modal = await Bun.file(new URL("./Modal.tsx", import.meta.url).pathname).text();
  css = await Bun.file(
    new URL("../../../styles/buttons-modal.css", import.meta.url).pathname,
  ).text();
});

describe("the armed ✕", () => {
  test("the close button carries the armed class while the guard is up", () => {
    // The class is conditional on `confirmClose` — the same state the footer
    // hint already keys off, so the two halves cannot disagree about whether the
    // guard is up.
    expect(modal).toContain('(confirmClose ? " is-armed" : "")');
  });

  test("…and the stylesheet actually gives that class a look", () => {
    // The half a typo would silently eat: a class nothing styles is exactly the
    // dead click this fixes. Pinned to the selector, and to the fact that it
    // repeats itself for :hover — without that, moving the mouse cools the
    // button back down while the guard is still live.
    expect(css).toContain(".deploy-close.is-armed");
    expect(css).toContain(".deploy-close.is-armed:hover:not(:disabled)");
  });

  test("it says what the next press will do, in the tooltip and to a screen reader", () => {
    // A screen reader has no corner to look at, so the accessible name carries
    // the state rather than only the colour doing it.
    expect(modal).toContain('"Press again to discard"');
    expect(modal).toContain('"Close and discard changes"');
    // …and it goes back to being an ordinary Close when the window lapses, with
    // the caller's own `closeTitle` still honoured on that side.
    expect(modal).toContain("(closeTitle ?? \"Close\")");
  });

  test("the footer hint stays — the button is an addition, not a replacement", () => {
    // Two channels on purpose: the button says WHERE, the hint says WHAT. The
    // hint is also `role="status"`, which is what announces the change at the
    // moment it happens.
    expect(modal).toContain("modal-dirty-hint");
    expect(modal).toContain("Unsaved changes — close again to discard");
    expect(modal).toContain('role="status"');
  });

  test("the two-step guard and its 2s window are unchanged", () => {
    // This pass is about visibility only. A first press on a dirty form still
    // arms rather than closes, and the arming still lapses after 2s so a form
    // left alone does not stay one stray click from being discarded.
    expect(modal).toContain("if (dirty && !confirmClose)");
    expect(modal).toContain("setConfirmClose(true)");
    expect(modal).toContain("setConfirmClose(false), 2000");
  });
});
