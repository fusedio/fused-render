// The dirty guard: its RULES (dirty-guard.ts) and its VISIBLE half (Modal.tsx).
//
// The guard itself — first close attempt arms, the next one discards — has been
// there since the chassis was written. What was missing is that the
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
import { decideClose, isDisarmingInteraction } from "./dirty-guard";

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

  test("the two-step guard is still two steps", () => {
    // A first press on a dirty form still arms rather than closes; the visible
    // state and the logical state are the same `confirmClose` flag, so the amber
    // cannot outlive (or predecease) what the next press will actually do.
    expect(modal).toContain("setConfirmClose(true)");
    expect(modal).toContain('decideClose({ busy, dirty, armed: confirmClose })');
  });
});

// The guard's RULES (dirty-guard.ts). Nothing below touches the clock, because
// the fix is precisely that the guard no longer consults one.
describe("the discard latch", () => {
  test("a first press on a dirty form arms instead of closing", () => {
    expect(decideClose({ busy: false, dirty: true, armed: false })).toBe("arm");
  });

  test("the next press discards — however long the user took over it", () => {
    // THE DEFECT THIS FIXES. The old guard disarmed on a 2s timer, so a press
    // arriving later than that re-armed and the modal never closed: QA measured
    // presses at 2.6s, 5.2s, 7.8s and 10.4s all leaving the dialog open. The
    // decision takes no elapsed time at all now, so there is no interval — long
    // or short — at which a second press fails to close.
    expect(decideClose({ busy: false, dirty: true, armed: true })).toBe("close");
  });

  test("a clean form closes on the first press", () => {
    expect(decideClose({ busy: false, dirty: false, armed: false })).toBe("close");
  });

  test("busy blocks the close outright, armed or not", () => {
    // `busy` outranks the guard: an action is running that must not be abandoned.
    expect(decideClose({ busy: true, dirty: true, armed: false })).toBe("block");
    expect(decideClose({ busy: true, dirty: true, armed: true })).toBe("block");
    expect(decideClose({ busy: true, dirty: false, armed: false })).toBe("block");
  });
});

describe("what disarms the guard", () => {
  test("going back to the form disarms it", () => {
    // Typing, or a pointer press on something that is not the ✕. This is the
    // user answering the question the other way — after it, the next ✕ press
    // arms again rather than discarding.
    expect(isDisarmingInteraction("a", false)).toBe(true);
    expect(isDisarmingInteraction(null, false)).toBe(true);
  });

  test("pressing the ✕ again does not disarm it", () => {
    // pointerdown on the ✕ arrives BEFORE its click. If that disarmed, the
    // second press would re-arm and we would be back in the same loop.
    expect(isDisarmingInteraction(null, true)).toBe(false);
    expect(isDisarmingInteraction("Enter", true)).toBe(false);
  });

  test("Escape does not disarm — it IS the second press", () => {
    expect(isDisarmingInteraction("Escape", false)).toBe(false);
  });

  test("tabbing back to the ✕ does not disarm en route", () => {
    // A keyboard user arms with Esc, then Tabs to the ✕ to press it. Treating
    // that navigation as "still editing" would make the keyboard path loop the
    // way the timer used to.
    expect(isDisarmingInteraction("Tab", false)).toBe(false);
    expect(isDisarmingInteraction("Shift", false)).toBe(false);
  });
});

describe("the timer is gone", () => {
  test("nothing in the chassis disarms on a clock", () => {
    // The regression guard: reintroducing any timed disarm brings the loop back.
    expect(modal).not.toContain("setConfirmClose(false), 2000");
    expect(modal).not.toContain("confirmTimer");
    // The only remaining timing in the modal is the exit animation, which is
    // owned by useDeferredClose/OVERLAY_EXIT_MS, not by the guard.
    expect(modal).not.toMatch(/setTimeout\([^)]*setConfirmClose/);
  });

  test("the disarm listeners are bound to interaction, not to time", () => {
    expect(modal).toContain('dialog.addEventListener("pointerdown", disarm)');
    expect(modal).toContain('dialog.addEventListener("input", disarm)');
    expect(modal).toContain('dialog.addEventListener("change", disarm)');
    expect(modal).toContain('dialog.addEventListener("keydown", onKey)');
  });
});
