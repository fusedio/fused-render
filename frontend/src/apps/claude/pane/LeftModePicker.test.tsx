// THE PICKER'S CLOSE BEHAVIOUR (P3-29, T:5549-5555).
//
// One rule with a reason T states outright: "Only when the close was the user's
// own keystroke: stealing focus back on an outside click would yank it off
// whatever they actually clicked." Base UI returns focus to the trigger on
// EVERY close by default, so clicking into the transcript put the caret back
// onto this pill — which is a real bug in the one flow the picker exists for
// (pick a view, then go back to typing).
//
// Read off the popup's `finalFocus` prop rather than through a real close: the
// suite has no CSSOM and no pointer, and what shipped IS the predicate. Driving
// Base UI's own close paths would test Base UI.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { LeftModePicker } = await import("./LeftModePicker");
const { createMemoryParamsStore } = await import("../params/store");

const MODES = [
  { mode: "view", icon: "", label: "View" },
  { mode: "code", icon: "", label: "Code" },
] as unknown as Parameters<typeof LeftModePicker>[0]["modes"];

function mount(leftMode = "view") {
  const params = createMemoryParamsStore({ leftmode: leftMode });
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<LeftModePicker modes={MODES} params={params} leftMode={leftMode} />);
  });
  return { r, params };
}

/** The popup's `finalFocus`, whichever node Base UI's content ends up on. */
function finalFocus(r: ReturnType<typeof create>): (t: string) => boolean {
  const node = r.root.findAll(
    (n) => typeof (n.props as { finalFocus?: unknown }).finalFocus === "function",
  )[0];
  expect(node, "no finalFocus on the picker's popup").toBeTruthy();
  return (node!.props as { finalFocus(t: string): boolean }).finalFocus;
}

test("a KEYSTROKE close hands focus back; an outside press does not (T:5549)", () => {
  const { r } = mount();
  const decide = finalFocus(r);
  // Escape, Enter, an arrow-key pick: the user is on the keyboard and losing
  // focus into nowhere would strand them.
  expect(decide("keyboard")).toBe(true);
  // A press outside — the transcript, the composer, anything: focus stays where
  // the pointer just put it. `false` is what Base UI's own default gets wrong.
  for (const kind of ["mouse", "touch", "pen", "pointer", "none"]) {
    expect(decide(kind), kind + " must not steal focus").toBe(false);
  }
});

test("the picker is chrome only when there is a choice (T:5560-5562)", () => {
  // The other half of the same seam: a one-item picker cannot do anything, so
  // there is no popup to have a close rule at all.
  const params = createMemoryParamsStore({ leftmode: "view" });
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <LeftModePicker
        modes={[MODES[0]!] as unknown as typeof MODES}
        params={params}
        leftMode="view"
      />,
    );
  });
  expect(r.toJSON()).toBeNull();
});
