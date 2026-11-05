import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { BreakoutSelectionBar } = await import("./BreakoutSelectionBar");

const noop = () => {};
const html = (count: number) => renderToStaticMarkup(createElement(BreakoutSelectionBar, { count, onOpen: noop, onClear: noop }));

describe("BreakoutSelectionBar", () => {
  test("singular and plural", () => {
    expect(html(1)).toContain("1 exchange picked");
    expect(html(3)).toContain("3 exchanges picked");
  });
  test("renders nothing at zero", () => {
    expect(html(0)).toBe("");
  });
  test("buttons call their handlers", () => {
    const calls: string[] = [];
    let r: ReturnType<typeof create>;
    act(() => {
      r = create(createElement(BreakoutSelectionBar, { count: 2, onOpen: () => calls.push("open"), onClear: () => calls.push("clear") }));
    });
    const find = (cls: string) => r!.root.findByProps({ className: cls });
    act(() => find("breakout-sel-open").props.onClick());
    act(() => find("breakout-sel-clear").props.onClick());
    expect(calls).toEqual(["open", "clear"]);
  });
});
