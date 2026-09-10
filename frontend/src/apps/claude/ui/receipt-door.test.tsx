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
import { historyToTurns } from "../protocol/history";
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

/** The transcript stat every history payload carries; nothing here reads it. */
const STAT = { path: "/t.jsonl", mtime: 1, size: 2 };

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
  //
  // HAND-BUILT, because R1-1 made this shape unreachable for an app-state wire:
  // neither producer of a `UserTurn` leaves the flag off when the block is
  // there any more (the live path never did; `historyToTurns` now does not
  // either — see below). What it still pins is `Turn`'s own rule, which is what
  // a future receipt-less wire will land on.
  const r = mount(<Turn turn={user({ raw: WIRE })} onShowSent={() => {}} />);
  expect(find(r, "attach")).toHaveLength(0);
  expect(find(r, "sentbtn")).toHaveLength(0);
});

// R1-1: THE SAME DOOR AFTER A RELOAD.
//
// The door is the receipt, so a turn without the receipt has no entrance at all
// now that the hover control is gone (the test above). A reload's turns come out
// of `historyToTurns`, and that mapper never wrote `appState` — its only writer
// was the live path — while `stripBlocks` DID take the block off the display
// text. So a restored app-state turn had `raw !== text`, no receipt line, and no
// door: the wire the message actually sent was unreachable from the UI, and the
// "app state attached" line vanished across a reload. That is precisely the
// live-vs-restored divergence PR2's one-builder rule exists to prevent, and no
// test caught it because every case above mounts a LIVE turn. So this one drives
// the real mapper with the payload the server returns and mounts what it made.
test("a RESTORED app-state turn keeps its receipt, and it is still the door (R1-1)", () => {
  const seen: UserTurn[] = [];
  const turns = historyToTurns({ turns: [{ role: "user", text: WIRE, uuid: "abc-123" }], transcript: STAT });
  const turn = turns[0] as UserTurn;

  // The mapper's half: the block is off the bubble, kept on the wire, and the
  // receipt flag is read FROM that wire.
  expect(turn.text).toBe("what is the app title?");
  expect(turn.raw).toBe(WIRE);
  expect(turn.appState).toBe(true);

  const r = mount(<Turn turn={turn} onShowSent={(t) => seen.push(t)} />);
  const attach = find(r, "attach");
  expect(attach).toHaveLength(1);
  // A BUTTON and not the inert caption: `raw !== text`, so there is a room
  // behind it — the room that used to be reachable live and lost on reload.
  expect(attach[0].type).toBe("button");
  expect((attach[0].children ?? []).join("")).toBe("app state attached");
  expect((attach[0].props as { title?: string }).title).toBe(
    "Click to see exactly what was sent to the agent",
  );
  expect(find(r, "sentbtn")).toHaveLength(0);

  act(() => {
    (attach[0].props as { onClick: () => void }).onClick();
  });
  // The whole restored turn, so the panel reads the right `raw` off it.
  expect(seen).toEqual([turn]);
});

test("a restored turn with NO app-state block owes no receipt (R1-1)", () => {
  // Read off the block, never assumed: a plain typed message comes back plain,
  // so the line cannot turn up claiming an attachment there is none of.
  const turns = historyToTurns({ turns: [{ role: "user", text: "what is the app title?", uuid: "abc-124" }], transcript: STAT });
  expect((turns[0] as UserTurn).appState).toBeUndefined();
  const r = mount(<Turn turn={turns[0] as UserTurn} onShowSent={() => {}} />);
  expect(find(r, "attach")).toHaveLength(0);
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
