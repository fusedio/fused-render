// The streaming tail on screen: the caret, and which text segment the typer
// owns. `T` shows `span.cursor` "▋" beside the growing prose for the whole of a
// live turn (T:15063-15066); the port showed it nowhere, because the typer only
// ever drove a flat bubble and no component rendered a caret at all.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { Turn as TurnRow } from "../protocol/controller-api";
import { lastErrorKey } from "./Transcript";
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
function walk(node: Json | string | null, hit: (n: Json) => void): void {
  if (!node || typeof node === "string") return;
  hit(node);
  for (const k of node.children ?? []) walk(k as Json, hit);
}
function withClass(r: ReturnType<typeof create>, cls: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    const c = (n.props as { className?: string } | undefined)?.className;
    if (typeof c === "string" && c.split(/\s+/).includes(cls)) out.push(n);
  });
  return out;
}
/** The prose each MarkdownView rendered, tags stripped: what is under test is
 *  WHICH string reached the funnel, not how `marked` wrapped it (and the DOM
 *  shim's sanitizer wraps it differently from a browser's). */
function proses(r: ReturnType<typeof create>): string[] {
  return htmls(r).map((h) => h.replace(/<[^>]*>/g, "").trim());
}

function htmls(r: ReturnType<typeof create>): string[] {
  const out: string[] = [];
  walk(r.toJSON() as Json, (n) => {
    const h = (n.props as { dangerouslySetInnerHTML?: { __html: string } } | undefined)
      ?.dangerouslySetInnerHTML;
    if (h) out.push(h.__html);
  });
  return out;
}

const assistant = (over: Partial<TurnRow> = {}): TurnRow =>
  ({ role: "assistant", key: "a:1", text: "", streaming: true, ...over }) as TurnRow;

test("the caret rides beside the growing prose, and only while it is growing", () => {
  const turn = assistant({ text: "hello there" });
  const live = mount(<Turn turn={turn} tail={{ index: -1, text: "hell", cursor: true }} />);
  // The typer's SLICE, not the state's full text: the whole point of the seam.
  expect(proses(live)).toEqual(["hell"]);
  expect(withClass(live, "cursor").length).toBe(1);

  // The frame that completed the drain retires the caret (T:15108 `cur.remove`).
  const done = mount(<Turn turn={turn} tail={{ index: -1, text: "hello there", cursor: false }} />);
  expect(withClass(done, "cursor").length).toBe(0);

  // And a settled turn has no tail at all, so it renders its own text.
  const settled = mount(<Turn turn={assistant({ text: "hello there", streaming: false })} />);
  expect(proses(settled)).toEqual(["hello there"]);
  expect(withClass(settled, "cursor").length).toBe(0);
});

test("in a segment turn only the TAIL segment is the typer's; the rest stay settled", () => {
  const turn = assistant({
    text: "onetwo",
    segments: [
      { kind: "text", text: "one" },
      { kind: "tool", id: "t1", tool: "Bash", status: "ok", input: {}, output: "" },
      { kind: "text", text: "two" },
    ],
  } as Partial<TurnRow>);
  const r = mount(<Turn turn={turn} tail={{ index: 2, text: "tw", cursor: true }} />);
  // The settled segment is its own text; the tail is the typer's slice.
  expect(proses(r)).toEqual(["one", "tw"]);
  expect(withClass(r, "cursor").length).toBe(1);
});

test("a turn that ended on a tool call is PARKED: no caret anywhere (T:15057-15062)", () => {
  const turn = assistant({
    text: "one",
    segments: [
      { kind: "text", text: "one" },
      { kind: "tool", id: "t1", tool: "Bash", status: "running", input: {}, output: null },
    ],
  } as Partial<TurnRow>);
  const r = mount(<Turn turn={turn} tail={null} />);
  expect(withClass(r, "cursor").length).toBe(0);
  expect(proses(r)).toEqual(["one"]);
});

test("an error row is T's plain red line, as a text node", () => {
  const r = mount(<Turn turn={{ role: "error", key: "e:1", text: "claude exited", kind: "generic" }} />);
  expect(withClass(r, "error").length).toBe(1);
  // Never markdown: the message is agent.py's or the CLI's bytes.
  expect(htmls(r)).toEqual([]);
});

test("lastErrorKey names the row the trouble card is already reporting", () => {
  const turns: TurnRow[] = [
    { role: "user", key: "u:1", text: "go" },
    { role: "error", key: "e:1", text: "first", kind: "generic" },
    { role: "user", key: "u:2", text: "again" },
    { role: "error", key: "e:2", text: "second", kind: "generic" },
  ];
  expect(lastErrorKey(turns)).toBe("e:2");
  expect(lastErrorKey(turns.slice(0, 3))).toBe("e:1");
  expect(lastErrorKey([turns[0]])).toBeNull();
  expect(lastErrorKey([])).toBeNull();
});

// ── AND THE TAIL A REPAIR LEAVES BEHIND (P4-10, renderer half) ─────────────
//
// The controller bumps `repaired` when a run that finished while the frame was
// away appends its turn; this is what the nonce is FOR. `follow.test.tsx` pins
// the OPPOSITE rule for the banner (a scrolled-up reader is not jumped), so
// nothing pinned this one until the effect moved out of `ClaudeChat`.
const { useRepairScroll } = await import("./useRepairScroll");

function fakeLog() {
  return { scrollTop: 0, scrollHeight: 1000 };
}

/** Drives the hook the way the chat does: one nonce prop, one root ref. */
function repairHarness(start = 0) {
  const log = fakeLog();
  const rootRef = { current: { querySelector: () => log } };
  let r!: ReturnType<typeof create>;
  function Probe({ repaired }: { repaired: number }) {
    useRepairScroll(repaired, rootRef);
    return null;
  }
  act(() => {
    r = create(<Probe repaired={start} />);
  });
  mounted.push(r);
  return {
    log,
    bump(to: number) {
      act(() => r.update(<Probe repaired={to} />));
    },
  };
}

test("A REPAIR SCROLLS TO THE TAIL whatever the reader was doing (T:17851)", () => {
  const h = repairHarness(0);
  // A reader who had scrolled up — which is exactly the reader this case is
  // about, one who came back to a run that finished while the frame was away.
  h.log.scrollTop = 200;
  h.bump(1);
  expect(h.log.scrollTop).toBe(1000);
});

test("A NONCE, NOT A FLAG: two repairs are two scrolls", () => {
  const h = repairHarness(0);
  h.bump(1);
  expect(h.log.scrollTop).toBe(1000);
  h.log.scrollTop = 40;
  h.bump(2);
  expect(h.log.scrollTop).toBe(1000);
});

test("a re-render at the SAME nonce is not a repair", () => {
  const h = repairHarness(0);
  h.bump(1);
  h.log.scrollTop = 40;
  h.bump(1);
  expect(h.log.scrollTop).toBe(40);
});

test("the MOUNT's own value is not a repair either", () => {
  // A remount over a controller that has already repaired three turns arrives
  // with a nonce of 3, and that is not a repair this reader watched happen.
  const h = repairHarness(3);
  expect(h.log.scrollTop).toBe(0);
  h.bump(4);
  expect(h.log.scrollTop).toBe(1000);
});

test("no scrollport is no crash: the effect is a no-op before the first paint", () => {
  function Probe({ repaired }: { repaired: number }) {
    useRepairScroll(repaired, { current: null });
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe repaired={0} />);
  });
  mounted.push(r);
  expect(() => act(() => r.update(<Probe repaired={1} />))).not.toThrow();
});
