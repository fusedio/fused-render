// THE ENTER KEY NAMES ITS BUTTON (Sina, 2026-09-20). The chassis focuses the
// footer's first button when a dialog opens; the ring that says so must not
// depend on `:focus-visible`, which Chrome withholds after a mouse click — that
// is how "Delete T020?" closed on Enter before the reader saw which button
// held focus. Source assertions, in the style of modal-dirty-guard.test.ts.
import { beforeAll, describe, expect, test } from "bun:test";

let css = "";
let modal = "";
beforeAll(async () => {
  css = await Bun.file(
    new URL("../../../styles/buttons-modal.css", import.meta.url).pathname,
  ).text();
  modal = await Bun.file(new URL("./Modal.tsx", import.meta.url).pathname).text();
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

  test("the destructive confirm is a solid button, the shape of .btn-primary", () => {
    const danger = rule(".btn-danger,\n.deploy-body .btn-danger,\n.prefs-section .btn-danger");
    expect(danger).toContain("background: var(--error)");
    expect(danger).toContain("color: var(--bg)");
    expect(danger).toContain("border-color: transparent");
    // The outlined red look is what read as "focused" next to an unringed Cancel.
    expect(danger).not.toContain("rgba(var(--error-rgb)");
  });

  test("the chassis still parks initial focus in the body/footer, past the head", () => {
    expect(modal).toContain('focusables.find((el) => !el.closest(".modal-head"))');
  });
});
