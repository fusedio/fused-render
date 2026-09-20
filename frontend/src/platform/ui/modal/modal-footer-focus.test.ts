// THE ENTER KEY NAMES ITS BUTTON (Sina, 2026-09-20). The chassis focuses the
// footer's first button when a dialog opens; the ring that says so must not
// depend on `:focus-visible`, which Chrome withholds after a mouse click — that
// is how "Delete T020?" closed on Enter before the reader saw which button
// held focus. Source assertions, in the style of modal-dirty-guard.test.ts.
import { beforeAll, describe, expect, test } from "bun:test";

let css = "";
let modal = "";
let erase = "";
beforeAll(async () => {
  css = await Bun.file(
    new URL("../../../styles/buttons-modal.css", import.meta.url).pathname,
  ).text();
  modal = await Bun.file(new URL("./Modal.tsx", import.meta.url).pathname).text();
  erase = await Bun.file(new URL("../EraseTaskModal.tsx", import.meta.url).pathname).text();
});

function rule(selector: string): string {
  const at = css.indexOf(selector + " {");
  expect(at, `${selector} rule missing`).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at));
}

describe("a focused footer button says so", () => {
  test("the ring is on :focus, not :focus-visible, inside the footer", () => {
    const ring = rule(".modal-footer .btn:focus");
    expect(ring).toContain("outline: 2px solid var(--accent)");
    expect(css).not.toContain(".modal-footer .btn:focus-visible {");
  });

  test("the destructive confirm is tinted red text on a light red fill, with no border", () => {
    const danger = rule(".btn-danger,\n.deploy-body .btn-danger,\n.prefs-section .btn-danger");
    expect(danger).toContain("background: rgba(var(--error-rgb), 0.12)");
    expect(danger).toContain("color: var(--error)");
    // The red BORDER is what read as a focus ring next to an unringed Cancel.
    expect(danger).toContain("border-color: transparent");
    expect(danger).not.toContain("border-color: var(--error)");
  });

  test("← and → walk the footer's buttons, wrapping", () => {
    expect(modal).toContain('if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;');
    expect(modal).toContain('querySelectorAll<HTMLButtonElement>("button:not(:disabled)")');
    expect(modal).toContain("buttons[(at + step + buttons.length) % buttons.length].focus()");
  });

  test("the chassis still parks initial focus in the body/footer, past the head", () => {
    expect(modal).toContain('focusables.find((el) => !el.closest(".modal-head"))');
  });

  test("the Delete task dialog opens with Delete forever focused", () => {
    expect(erase).toContain("initialFocus={confirmRef}");
    expect(erase).toMatch(/ref=\{confirmRef\}[\s\S]{0,120}className="btn btn-danger"/);
  });

  test("the confirm keeps focus while the erase runs: aria-disabled, never disabled", () => {
    const confirmBtn = erase.slice(erase.indexOf("ref={confirmRef}"), erase.indexOf("onClick={confirm}"));
    expect(confirmBtn).toContain("aria-disabled={busy}");
    expect(confirmBtn).not.toMatch(/\sdisabled=\{busy\}/);
  });

  test("the dialog card draws no ring when it is the focus fallback", () => {
    expect(rule(".modal-dialog:focus")).toContain("outline: none");
  });
});
