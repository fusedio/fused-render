import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { BreakoutSelectionBar } = await import("./BreakoutSelectionBar");

const noop = () => {};
const html = (count: number) => renderToStaticMarkup(createElement(BreakoutSelectionBar, { count, onStart: noop, onCancel: noop }));

describe("BreakoutSelectionBar", () => {
  test("singular and plural, with the number bold", () => {
    expect(html(1)).toContain("<b>1</b> exchange picked");
    expect(html(3)).toContain("<b>3</b> exchanges picked");
  });
  test("zero shows the instruction", () => {
    expect(html(0)).toContain("Click messages to pick them for a breakout");
  });
  test("Start is disabled at zero and enabled otherwise", () => {
    expect(html(0)).toMatch(/breakout-sel-start[^>]*disabled/);
    expect(html(2)).not.toMatch(/breakout-sel-start[^>]*disabled/);
  });
  test("buttons call their handlers", () => {
    const calls: string[] = [];
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(createElement(BreakoutSelectionBar, { count: 2, onStart: () => calls.push("start"), onCancel: () => calls.push("cancel") }));
    });
    const find = (cls: string) => r.root.findByProps({ className: cls });
    act(() => find("breakout-sel-start").props.onClick());
    act(() => find("breakout-sel-cancel").props.onClick());
    expect(calls).toEqual(["start", "cancel"]);
  });
});
