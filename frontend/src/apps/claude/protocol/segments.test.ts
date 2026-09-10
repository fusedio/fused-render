// The segment model: dedupe by id, the growing tail, and D687 slicing.
import { describe, expect, test } from "bun:test";

import {
  cardKey,
  finishedTailText,
  parseTailKey,
  streamingTailOf,
  tailKey,
  pollBody,
  reconcileSegments,
  segText,
  tailIndex,
  viewKind,
} from "./segments";
import type { Segment, ToolSegment } from "./types";

const text = (t: string): Segment => ({ kind: "text", text: t });
const tool = (id: string, status: ToolSegment["status"], output: string | null = null): Segment => ({
  kind: "tool",
  id,
  name: "Bash",
  input: { command: "ls" },
  status,
  output,
  images: [],
});

describe("segText / viewKind", () => {
  test("a missing or non-string text is \"\"", () => {
    expect(segText(undefined)).toBe("");
    expect(segText({ kind: "text" } as unknown as Segment)).toBe("");
    expect(segText(text("hi"))).toBe("hi");
  });

  test("anything a newer agent.py invents renders as text (T:15656)", () => {
    expect(viewKind({ kind: "sparkle", text: "x" } as unknown as Segment)).toBe("text");
    expect(viewKind(tool("t1", "ok"))).toBe("tool");
    expect(viewKind({ kind: "notice", text: "x", status: "" })).toBe("notice");
  });
});

describe("cardKey (T:15234)", () => {
  test("a tool call is keyed by its tool_use id, stable across re-renders", () => {
    expect(cardKey(3, tool("abc", "running"), 7)).toBe("tool:abc");
  });
  test("everything else is keyed by position inside a numbered container", () => {
    expect(cardKey(3, text("x"), 7)).toBe("3:7");
    // A tool call with no id falls back to position too (id "" is not an id).
    expect(cardKey(3, { ...(tool("", "ok") as ToolSegment) }, 1)).toBe("3:1");
  });
});

describe("the growing tail (T:15664)", () => {
  test("only a text segment at the very END is growing", () => {
    expect(tailIndex([text("a"), tool("t", "ok"), text("b")])).toBe(2);
    expect(tailIndex([text("a"), tool("t", "running")])).toBe(-1);
    expect(tailIndex([])).toBe(-1);
  });
});

describe("reconcileSegments dedupes the replay", () => {
  test("one tool_use id keeps its FIRST position and its LATEST payload", () => {
    const view = reconcileSegments(1, [
      tool("t1", "running"),
      text("prose"),
      tool("t1", "ok", "done"),
    ]);
    expect(view.rows.map((r) => r.key)).toEqual(["tool:t1", "1:1"]);
    expect((view.rows[0].seg as ToolSegment).status).toBe("ok");
    expect((view.rows[0].seg as ToolSegment).output).toBe("done");
  });

  test("the tail is marked, and its text is what finish() gets", () => {
    const view = reconcileSegments(1, [tool("t1", "ok"), text("tail")]);
    expect(view.tail).toBe(1);
    expect(view.rows[1].tail).toBe(true);
    expect(view.tailText).toBe("tail");
  });

  test("a turn that ends on a tool call has no tail text", () => {
    const view = reconcileSegments(1, [text("a"), tool("t1", "running")]);
    expect(view.tail).toBe(-1);
    expect(view.tailText).toBeNull();
  });

  test("polling the same list twice is identical (idempotent)", () => {
    const list = [tool("t1", "ok"), text("a")];
    expect(reconcileSegments(9, list)).toEqual(reconcileSegments(9, list));
  });

  test("holes and nulls in the payload are dropped, never rendered", () => {
    const view = reconcileSegments(1, [null as unknown as Segment, text("a")]);
    expect(view.rows.length).toBe(1);
  });
});

describe("pollBody: segments win over the flat legacy text (T:16255)", () => {
  test("segments present ⇒ the flat text is not rendered as well", () => {
    const body = pollBody([text("real")], "real", 1);
    expect(body.mode).toBe("segments");
    expect(body.text).toBe("");
  });

  test("no segments yet ⇒ the legacy text path, so the first poll is not blank", () => {
    const body = pollBody([], "streaming…", 1);
    expect(body.mode).toBe("text");
    expect(body.text).toBe("streaming…");
  });

  test("nothing at all ⇒ no bubble", () => {
    expect(pollBody([], "", 1).mode).toBe("empty");
    expect(pollBody(undefined, undefined, 1).mode).toBe("empty");
  });

  // The D687 seam used to be applied HERE, from a `segBase`/`textBase` pair the
  // poll loop froze when a follow-up landed. It is not a decision this file can
  // make — the seam comes from the poll (`turn_breaks`) and the caller slices
  // the payload to a span before asking for a body — so all `pollBody` has to
  // keep proving is that a span behaves like any other payload.
  test("a sliced span renders as its own body, with no base of its own", () => {
    const segs = [text("before"), tool("t1", "ok"), text("after")];
    const body = pollBody(segs.slice(2), "beforeafter".slice(6), 1);
    expect(body.mode).toBe("segments");
    expect(body.mode === "segments" && body.view.rows.length).toBe(1);
    expect(body.mode === "segments" && body.view.tailText).toBe("after");
    const flat = pollBody([], "beforeafter".slice(6), 1);
    expect(flat.mode === "text" && flat.text).toBe("after");
  });
});

// ── the streaming tail seam (T:15664-15683, 15057-15062) ────────────────────
//
// This is the decision that used to be missing entirely: the typer only ever
// drove a FLAT bubble, so a turn with segments — which is every turn that calls
// a tool — streamed nothing and showed no caret. These are the three cases the
// paint side keys the typer on.
describe("streamingTailOf: where the typer belongs", () => {
  const live = (over: Record<string, unknown> = {}) => ({
    role: "assistant",
    key: "a:1",
    text: "flat",
    streaming: true,
    ...over,
  });

  test("no segments ⇒ the flat body, index -1 (T:13486-13504)", () => {
    expect(streamingTailOf(live())).toEqual({
      turnKey: "a:1",
      index: -1,
      key: "a:1#-1",
      text: "flat",
    });
  });

  test("a trailing text segment IS the tail, and its own text is the target", () => {
    const segments = [text("one"), tool("t1", "ok"), text("two")];
    expect(streamingTailOf(live({ segments }))).toEqual({
      turnKey: "a:1",
      index: 2,
      key: "a:1#2",
      text: "two",
    });
  });

  test("a turn ending on a tool call PARKS the typer: no element, no caret", () => {
    expect(streamingTailOf(live({ segments: [text("one"), tool("t1", "running")] }))).toBeNull();
  });

  test("a thinking block at the end parks it too — the tail is not prose", () => {
    const segments = [text("one"), { kind: "thinking", text: "hmm" } as Segment];
    expect(streamingTailOf(live({ segments }))).toBeNull();
  });

  test("nothing streams for a settled turn, a user turn, or nothing at all", () => {
    expect(streamingTailOf(live({ streaming: false }))).toBeNull();
    expect(streamingTailOf({ role: "user", key: "u:1", text: "hi", streaming: true })).toBeNull();
    expect(streamingTailOf(null)).toBeNull();
    expect(streamingTailOf(undefined)).toBeNull();
  });

  test("the key changes when the typer must RETARGET, not when text grows", () => {
    const a = streamingTailOf(live({ segments: [text("one")] }))!;
    const grown = streamingTailOf(live({ segments: [text("one and more")] }))!;
    // same element, more text ⇒ update()
    expect(grown.key).toBe(a.key);
    expect(grown.text).not.toBe(a.text);
    // the tail moved past a tool call ⇒ retarget()
    const moved = streamingTailOf(live({ segments: [text("one"), tool("t1", "ok"), text("two")] }))!;
    expect(moved.key).not.toBe(a.key);
    // a different turn ⇒ retarget()
    expect(streamingTailOf(live({ key: "a:2", segments: [text("one")] }))!.key).not.toBe(a.key);
  });

  test("the key round-trips: turn keys carry no `#` of their own", () => {
    expect(parseTailKey(tailKey("a:12", 3))).toEqual({ turnKey: "a:12", index: 3 });
    expect(parseTailKey(tailKey("a:12", -1))).toEqual({ turnKey: "a:12", index: -1 });
    // a history uuid is a turn key too
    const uuid = "2f9c1e40-0000-4000-8000-abcdefabcdef";
    expect(parseTailKey(tailKey(uuid, 0))).toEqual({ turnKey: uuid, index: 0 });
  });
});

describe("finishedTailText: what typer.finish drains to (T:16336)", () => {
  test("a segment turn drains to its trailing prose", () => {
    const turn = { role: "assistant", key: "a:1", text: "onetwo", segments: [text("one"), tool("t1", "ok"), text("two")] };
    expect(finishedTailText(turn)).toBe("two");
  });

  test("a flat turn drains to its flat text", () => {
    expect(finishedTailText({ role: "assistant", key: "a:1", text: "all of it" })).toBe("all of it");
  });

  test("a turn that ended on a tool call has nothing left to drain — `tailText || \"\"`", () => {
    const turn = { role: "assistant", key: "a:1", text: "one", segments: [text("one"), tool("t1", "ok")] };
    expect(finishedTailText(turn)).toBe("");
  });

  test("a row that is not an assistant turn drains to nothing, never throws", () => {
    expect(finishedTailText({ role: "note", key: "n:1", text: "read app state" })).toBe("");
    expect(finishedTailText(null)).toBe("");
  });
});

describe("an unchanged chip keeps its OBJECT across polls (T:15549-15554)", () => {
  test("same id, status, output and image count → the identical `seg` reference", () => {
    // The point is `===`, not deep equality: `ToolChip` is `memo`'d and `seg`
    // is its only interesting prop, so a fresh object every poll meant a `Write`
    // chip re-serialised its whole `<pre>` body 2.5×/s for the length of the
    // turn — with the `JSON.stringify` of its uncapped `content` alongside.
    const first = reconcileSegments(1, [tool("t1", "running")]);
    const again = reconcileSegments(1, [tool("t1", "running")], first);
    expect(again.rows[0]!.seg).toBe(first.rows[0]!.seg);
  });

  test("`input` is NOT in the identity — T excludes it, and says why", () => {
    // T: "`input` is deliberately NOT in the key. It cannot change under a live
    // chip […] and a Write's `content` is uncapped, so keying on it would
    // re-stringify the whole file being written on every 400 ms poll."
    const first = reconcileSegments(1, [tool("t1", "running")]);
    const grown = {
      ...(tool("t1", "running") as ToolSegment),
      input: { command: "ls", content: "x".repeat(10000) },
    };
    const again = reconcileSegments(1, [grown as Segment], first);
    expect(again.rows[0]!.seg).toBe(first.rows[0]!.seg);
  });

  test("a status, output or image-count change DOES take the new object", () => {
    const first = reconcileSegments(1, [tool("t1", "running")]);
    for (const changed of [
      tool("t1", "ok"),
      tool("t1", "running", "some output"),
      { ...(tool("t1", "running") as ToolSegment), images: [{ media_type: "image/png", data: "iVBOR" }] } as Segment,
    ]) {
      const again = reconcileSegments(1, [changed], first);
      expect(again.rows[0]!.seg).not.toBe(first.rows[0]!.seg);
      expect(again.rows[0]!.seg).toBe(changed);
    }
  });

  test("a TEXT row always takes the new object — its body IS the content", () => {
    // "Unchanged" for a growing tail would have to be a deep comparison of the
    // very string that is growing, which is the opposite of cheap.
    const first = reconcileSegments(1, [text("hel")]);
    const again = reconcileSegments(1, [text("hello")], first);
    expect(again.rows[0]!.seg).not.toBe(first.rows[0]!.seg);
    // …and an IDENTICAL text row is still a new object: nothing is claimed
    // about it either way.
    const same = reconcileSegments(1, [text("hel")], first);
    expect(same.rows[0]!.seg).not.toBe(first.rows[0]!.seg);
  });

  test("a different id at the same key is never carried over", () => {
    const first = reconcileSegments(1, [tool("t1", "running")]);
    const other = reconcileSegments(1, [tool("t2", "running")], first);
    expect(other.rows[0]!.seg).not.toBe(first.rows[0]!.seg);
  });

  test("no `prev` at all behaves exactly as before", () => {
    const view = reconcileSegments(1, [tool("t1", "ok"), text("tail")]);
    expect(view.rows).toHaveLength(2);
    expect(view.rows[0]!.kind).toBe("tool");
    expect(view.tailText).toBe("tail");
  });

  test("`pollBody` passes it through", () => {
    const a = pollBody([tool("t1", "running")], "", 1);
    const b = pollBody([tool("t1", "running")], "", 1, a.view);
    expect(a.mode).toBe("segments");
    expect(b.mode).toBe("segments");
    expect(b.view!.rows[0]!.seg).toBe(a.view!.rows[0]!.seg);
  });
});
