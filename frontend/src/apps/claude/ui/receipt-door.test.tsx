// R4-1: THE RECEIPT IS THE DOOR.
//
// A receipt row under a bubble (a picture, a note) says the message carried
// more than the bubble shows, so it is the line that opens the panel
// showing that "more" — the affordance T ships (T:11059 `row.title = "Click to
// see exactly what was sent to the agent"`, T:1249). What this pins:
//
//   * a turn with a receipt AND a differing wire draws the receipt as a real
//     `button` carrying that title, and no second "what was sent" control;
//   * pressing it hands the WHOLE turn to `onShowSent` (the panel reads `raw`
//     off it, so the wrong turn would show the wrong message);
//   * a receipt with nothing extra behind it stays inert text — a door into a
//     room that is just the bubble again would be a lie;
//   * a turn with a differing wire but NO receipt has no door at all — the
//     hover control that used to stand in for one is gone everywhere (P3R1-7);
//   * and the panel itself wears the shared modal chassis — the Delete task
//     dialog's chrome — rather than a dialog skin of its own.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { UserTurn } from "../protocol/controller-api";
import { annStanza } from "../protocol/wire";
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

// 2026-09-20 (Akshil): the app-state receipt line is gone. A message that
// carried a `<live-app-state>` block draws the words and nothing under them —
// no "app state attached" caption, as a button or as text — whether or not a
// panel host is mounted.
test("an app-state send draws NO receipt line at all", () => {
  const turn = user({ raw: WIRE, appState: true });
  const withHost = mount(<Turn turn={turn} onShowSent={() => {}} />);
  expect(find(withHost, "attach")).toHaveLength(0);
  expect(find(withHost, "sentbtn")).toHaveLength(0);
  const noHost = mount(<Turn turn={turn} />);
  expect(find(noHost, "attach")).toHaveLength(0);
  expect(JSON.stringify(noHost.toJSON())).not.toContain("app state attached");
});


// P3R1-7: an ANNOTATED send's receipt rows are the door, so the hover control
// has nothing left to do there.
const ANN_WIRE =
  "look at this\n<annotations>\n" +
  annStanza({ label: "A", content: "make this bigger", tag: "button#go" }) +
  "\n</annotations>";

test("annotation receipts are the door — no second hover control (P3R1-7)", () => {
  const seen: UserTurn[] = [];
  const turn = user({ text: "look at this", raw: ANN_WIRE });
  const r = mount(
    <Turn turn={turn} onOpenShot={() => {}} onShowSent={(t) => seen.push(t)} />,
  );

  // The rows exist and each is pressable, carrying T's own sentence
  // (T:11059/11072) — the affordance the owner expected to be the only one.
  const rows = find(r, "annsum-note");
  expect(rows.length).toBeGreaterThan(0);
  expect((rows[0].props as { title?: string }).title).toBe(
    "Click to see exactly what was sent to the agent",
  );
  // ONE DOOR: the hover "what was sent" button is gone.
  expect(find(r, "sentbtn")).toHaveLength(0);

  act(() => {
    (rows[0].props as { onClick: () => void }).onClick();
  });
  expect(seen).toEqual([turn]);
});

test("no receipt line at all → NO door, and no hover control either (P3R1-7)", () => {
  // R4-1 kept a `.sentbtn` for exactly this turn: a differing wire with no
  // receipt line to press. The owner took it out everywhere (2026-09-10) — a
  // word-shaped affordance that materialises under the pointer, names no turn in
  // particular, and doubles the receipt's own door wherever there is one. T
  // ships no such control. So this turn simply has no entrance, which is the
  // honest answer: there is nothing drawn under it to make one out of.
  const r = mount(<Turn turn={user({ raw: WIRE })} onShowSent={() => {}} />);
  expect(find(r, "attach")).toHaveLength(0);
  expect(find(r, "sentbtn")).toHaveLength(0);
});

test("the hover control is gone from the SOURCE and the SHEET (P3R1-7)", () => {
  const src = readFileSync(join(import.meta.dir, "Turn.tsx"), "utf8");
  expect(src).not.toContain('className="sentbtn"');
  expect(src).not.toContain("what was sent<");
  for (const sheet of ["../styles/transcript.css", "../styles/chat.css"]) {
    const css = readFileSync(join(import.meta.dir, sheet), "utf8");
    // The name survives only in the note that says why it is gone.
    expect(css).not.toContain(".sentbtn {");
    expect(css).not.toContain(".sentbtn,");
    expect(css).not.toContain(".sentbtn:");
  }
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
