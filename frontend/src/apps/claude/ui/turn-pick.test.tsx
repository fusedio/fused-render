// Breakout pick mode on a turn: the whole exchange is the click target, and the
// per-message Breakout button is gone.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { Turn } = await import("./Turn");

import type { AssistantTurn, UserTurn } from "../protocol/controller-api";
import { pickPlan, type TurnPick } from "./TurnPick";

const base: UserTurn = { role: "user", key: "k1", text: "hello", uuid: "u1" };
const answer: AssistantTurn = { role: "assistant", key: "k2", text: "hi there" };
const mk = (over: Partial<TurnPick> = {}): TurnPick => ({ checked: false, locked: false, lead: true, label: "hello", onToggle: () => {}, ...over });
const html = (turn: UserTurn | AssistantTurn, pick?: TurnPick) =>
  renderToStaticMarkup(createElement(Turn, { turn, ...(pick ? { pick } : {}) }));

describe("Turn pick mode", () => {
  test("no per-message breakout button remains", () => {
    expect(html(base, mk())).not.toContain("turn-breakout");
    expect(html(answer, mk({ lead: false }))).not.toContain("turn-breakout");
    expect(html(base)).not.toContain("is-pick");
  });
  test("the prompt turn carries an accessible check, the answer does not", () => {
    const out = html(base, mk({ checked: true }));
    expect(out).toContain('role="checkbox"');
    expect(out).toContain('aria-checked="true"');
    expect(out).toContain("hello");
    expect(out).toContain("is-pick-on");
    expect(html(answer, mk({ lead: false }))).not.toContain('role="checkbox"');
  });
  test("a locked exchange is marked and its check is not focusable", () => {
    const out = html(base, mk({ locked: true }));
    expect(out).toContain("is-pick-locked");
    expect(out).toContain('tabindex="-1"');
  });
  test("a click toggles; a click on a link or button inside does not; a locked one never does", () => {
    let n = 0;
    const press = (pick: TurnPick, target: unknown) => {
      let r!: ReturnType<typeof create>;
      act(() => {
        r = create(createElement(Turn, { turn: answer, pick }));
      });
      const root = r.root.findByProps({ className: r.root.findAll((x) => typeof x.props.className === "string" && x.props.className.includes("is-pick"))[0]!.props.className });
      act(() => root.props.onClick({ target }));
    };
    const el = (hit: boolean) => ({ closest: () => (hit ? {} : null) });
    press(mk({ lead: false, onToggle: () => n++ }), el(false));
    expect(n).toBe(1);
    press(mk({ lead: false, onToggle: () => n++ }), el(true));
    expect(n).toBe(1);
    press(mk({ lead: false, locked: true, onToggle: () => n++ }), el(false));
    expect(n).toBe(1);
  });
  test("Space and Enter on the check toggle", () => {
    let n = 0;
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(createElement(Turn, { turn: base, pick: mk({ onToggle: () => n++ }) }));
    });
    const chk = r.root.findByProps({ role: "checkbox" });
    const ev = (key: string) => ({ key, preventDefault() {}, stopPropagation() {} });
    act(() => chk.props.onKeyDown(ev(" ")));
    act(() => chk.props.onKeyDown(ev("Enter")));
    act(() => chk.props.onKeyDown(ev("a")));
    expect(n).toBe(2);
  });
});

describe("pickPlan", () => {
  const turns = [
    { key: "u1", role: "user", text: "one" },
    { key: "a1", role: "assistant" },
    { key: "u2", role: "user", text: "two" },
    { key: "a2", role: "assistant", streaming: true },
  ];
  test("a streaming turn locks its whole exchange; the other stays pickable", () => {
    const plan = pickPlan(turns, new Set(["u1", "a1"]), () => {});
    expect(plan.get("u1")!.locked).toBe(false);
    expect(plan.get("u1")!.checked).toBe(true);
    expect(plan.get("u1")!.lead).toBe(true);
    expect(plan.get("a1")!.lead).toBe(false);
    expect(plan.get("u2")!.locked).toBe(true);
    expect(plan.get("a2")!.locked).toBe(true);
  });
  test("a pending prompt locks its exchange", () => {
    const plan = pickPlan([{ key: "u", role: "user", text: "x", pending: true }], new Set(), () => {});
    expect(plan.get("u")!.locked).toBe(true);
  });
});
