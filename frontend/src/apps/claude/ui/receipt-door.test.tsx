// R4-1: THE RECEIPT IS THE DOOR.
//
// "app state attached" is the only line under a bubble that says the message
// carried more than the bubble shows, so it is the line that opens the panel
// showing that "more" — the affordance T ships (T:11059 `row.title = "Click to
// see exactly what was sent to the agent"`, T:1249). What this pins:
//
//   * a turn with a receipt AND a differing wire draws the receipt as a real
//     `button` carrying that title, and no second "what was sent" control;
//   * pressing it hands the WHOLE turn to `onShowSent` (the panel reads `raw`
//     off it, so the wrong turn would show the wrong message);
//   * a receipt with nothing extra behind it stays inert text — a door into a
//     room that is just the bubble again would be a lie;
//   * a turn with a differing wire but NO receipt keeps the hover control,
//     because there is no receipt line to press;
//   * and the panel itself wears the shared modal chassis — the Delete task
//     dialog's chrome — rather than a dialog skin of its own.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { UserTurn } from "../protocol/controller-api";
import { Turn } from "./Turn";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
function walk(node: Json | string | null, hit: (n: Json) => void) {
  if (!node || typeof node === "string") return;
  hit(node);
  for (const k of node.children ?? []) walk(k as Json, hit);
}
const cls = (n: Json) =>
  String((n.props as { className?: string }).className ?? "").split(/\s+/);
function find(r: ReturnType<typeof create>, klass: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes(klass)) out.push(n);
  });
  return out;
}

function user(over: Partial<UserTurn> = {}): UserTurn {
  return { role: "user", key: "u:1", text: "what is the app title?", ...over };
}

const WIRE = "what is the app title?\n<live-app-state>{\"title\":\"sine\"}</live-app-state>";

test("the receipt IS the button, and it is the only door (R4-1)", () => {
  const seen: UserTurn[] = [];
  const turn = user({ raw: WIRE, appState: true });
  const r = mount(<Turn turn={turn} onShowSent={(t) => seen.push(t)} />);

  const attach = find(r, "attach");
  expect(attach).toHaveLength(1);
  expect(attach[0].type).toBe("button");
  // The words are unchanged — this is still the receipt, not a new control.
  expect((attach[0].children ?? []).join("")).toBe("app state attached");
  // T's own hover copy, so the press is discoverable before it is made.
  expect((attach[0].props as { title?: string }).title).toBe(
    "Click to see exactly what was sent to the agent",
  );
  // ONE door: the separate hover control is gone for a turn with a receipt.
  expect(find(r, "sentbtn")).toHaveLength(0);

  act(() => {
    (attach[0].props as { onClick: () => void }).onClick();
  });
  // The whole turn, not just its text: the panel reads `raw` off it.
  expect(seen).toEqual([turn]);
});

test("a receipt with nothing extra behind it is not a door (R4-1)", () => {
  // Same text on the wire as in the bubble → the panel would show the bubble
  // back to the reader, so there is nothing to open.
  const r = mount(
    <Turn turn={user({ raw: "what is the app title?", appState: true })} onShowSent={() => {}} />,
  );
  const attach = find(r, "attach");
  expect(attach).toHaveLength(1);
  expect(attach[0].type).toBe("div");
  expect(find(r, "sentbtn")).toHaveLength(0);
});

test("no receipt, but hidden wire blocks → the hover control stays (R4-1)", () => {
  const seen: UserTurn[] = [];
  const turn = user({ raw: WIRE });
  const r = mount(<Turn turn={turn} onShowSent={(t) => seen.push(t)} />);
  expect(find(r, "attach")).toHaveLength(0);
  const btn = find(r, "sentbtn");
  expect(btn).toHaveLength(1);
  act(() => {
    (btn[0].props as { onClick: () => void }).onClick();
  });
  expect(seen).toEqual([turn]);
});

test("no host to open the panel → the receipt is plain text again (R4-1)", () => {
  const r = mount(<Turn turn={user({ raw: WIRE, appState: true })} />);
  const attach = find(r, "attach");
  expect(attach).toHaveLength(1);
  expect(attach[0].type).toBe("div");
  expect(find(r, "sentbtn")).toHaveLength(0);
});

test("the receipt keeps its 11px faint typography, as a button (T:1804)", () => {
  const sheet = readFileSync(join(import.meta.dir, "../styles/transcript.css"), "utf8");
  const base = sheet.slice(sheet.indexOf(".chat-root .turn.user .attach {"));
  expect(base.slice(0, base.indexOf("}"))).toContain("font-size: 11px");
  const door = sheet.slice(sheet.indexOf(".chat-root .turn.user button.attach {"));
  expect(door).not.toBe("");
  const body = door.slice(0, door.indexOf("}"));
  // A button reset, not a restyle: the caption must not arrive as a boxed
  // control with the UA's own font.
  expect(body).toContain("font-size: 11px");
  expect(body).toContain("cursor: pointer");
  expect(body).toContain("border: 0");
  expect(body).toContain("background: none");
  // …and it says it is pressable on hover AND on keyboard focus.
  expect(sheet).toContain(".chat-root .turn.user button.attach:focus-visible");
  expect(
    sheet.slice(sheet.indexOf(".chat-root .turn.user button.attach:hover")),
  ).toContain("text-decoration: underline");
});

test("the panel wears the app's modal chrome, not its own (R4-1)", () => {
  const src = readFileSync(join(import.meta.dir, "SentPop.tsx"), "utf8");
  // The chassis EraseTaskModal (the Delete task dialog) uses — overlay, card,
  // head ✕, focus trap, Esc/backdrop close, all of it once.
  expect(src).toContain('from "@platform/ui/modal/Modal"');
  // The shadcn dialog and the hand-rolled bar/pill it needed are gone.
  expect(src).not.toContain("shadcn/ui/dialog");
  expect(src).not.toContain("c-sentpop");
  // The one section PR1 has keeps its heading and its pre block.
  expect(src).toContain("Exact message the agent received");
  expect(src).toContain("c-sent-wire");
  // And nothing styles a box that no longer exists.
  const css = readFileSync(join(import.meta.dir, "../styles/composer.css"), "utf8");
  expect(css).not.toContain("c-sentpop");
});
