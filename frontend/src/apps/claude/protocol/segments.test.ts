// The segment model: dedupe by id, the growing tail, and D687 slicing.
import { describe, expect, test } from "bun:test";

import {
  cardKey,
  groupToolRuns,
  isToolRun,
  RUN_MIN,
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

describe("groupToolRuns (design.md §A)", () => {
  /** A tool with a name of its own, so a tally can be read off a run. */
  const named = (id: string, name: string, status: ToolSegment["status"] = "ok"): Segment => ({
    kind: "tool",
    id,
    name,
    input: {},
    status,
    output: "",
    images: [],
  });
  const think = (t: string): Segment => ({ kind: "thinking", text: t });
  /** Every segment back out in its original order — a run is a fold, never a
   *  filter, and this is what says nothing was dropped. */
  const flatten = (rows: ReturnType<typeof groupToolRuns>): Segment[] =>
    rows.flatMap((row) => (isToolRun(row) ? row.segs : [row]));

  test("RUN_MIN is 2 and a lone settled tool stays a chip", () => {
    expect(RUN_MIN).toBe(2);
    const segs = [text("a"), tool("t1", "ok"), text("b")];
    expect(groupToolRuns(segs)).toEqual(segs);
  });

  test("two or more consecutive settled tools fold into one run", () => {
    const segs = [text("a"), tool("t1", "ok"), tool("t2", "error"), tool("t3", "ok"), text("b")];
    const rows = groupToolRuns(segs);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toBe(segs[0]!);
    const run = rows[1]!;
    expect(isToolRun(run)).toBe(true);
    if (!isToolRun(run)) return;
    expect(run.start).toBe(1);
    expect(run.segs).toEqual([segs[1], segs[2], segs[3]] as ToolSegment[]);
    expect(rows[2]).toBe(segs[4]!);
    expect(flatten(rows)).toEqual(segs);
  });

  test("ONE running tool keeps the WHOLE stretch individual, settled ones included", () => {
    const segs = [tool("t1", "ok"), tool("t2", "ok"), tool("t3", "running")];
    expect(groupToolRuns(segs)).toEqual(segs);
    // ...and the stretch folds the moment it settles.
    const done = [tool("t1", "ok"), tool("t2", "ok"), tool("t3", "ok")];
    expect(groupToolRuns(done).filter(isToolRun)).toHaveLength(1);
  });

  test("text, thinking and notice each break a run", () => {
    for (const between of [text("mid"), think("mid"), { kind: "notice", text: "m", status: "" } as Segment]) {
      const segs = [tool("t1", "ok"), between, tool("t2", "ok")];
      expect(groupToolRuns(segs)).toEqual(segs);
    }
    // Two either side of the break are two runs, not one.
    const split = [tool("t1", "ok"), tool("t2", "ok"), text("mid"), tool("t3", "ok"), tool("t4", "ok")];
    const rows = groupToolRuns(split);
    expect(rows.filter(isToolRun)).toHaveLength(2);
    expect((rows.filter(isToolRun)[1] as { start: number }).start).toBe(3);
  });

  test("a filed card ends the run BEFORE its chip, which stays individual", () => {
    const segs = [
      tool("t1", "ok"),
      tool("t2", "ok"),
      tool("t3", "ok"),
      tool("t4", "ok"),
      tool("t5", "ok"),
    ];
    const rows = groupToolRuns(segs, new Map<number, unknown>([[2, "card"]]));
    expect(rows).toHaveLength(3);
    const first = rows[0]!;
    const last = rows[2]!;
    expect(isToolRun(first) && first.start === 0 && first.segs.length === 2).toBe(true);
    expect(rows[1]).toBe(segs[2]!);
    expect(isToolRun(last) && last.start === 3 && last.segs.length === 2).toBe(true);
    expect(flatten(rows)).toEqual(segs);
  });

  test("a card leaving fewer than RUN_MIN either side leaves plain chips", () => {
    const segs = [tool("t1", "ok"), tool("t2", "ok"), tool("t3", "ok")];
    expect(groupToolRuns(segs, new Map<number, unknown>([[1, "card"]]))).toEqual(segs);
  });

  test("ORIGINAL indices survive, so cardKey and cardsAfter still line up", () => {
    const segs = [
      text("a"),
      named("t1", "Read"),
      named("t2", "Bash"),
      think("why"),
      named("t3", "Read"),
      named("t4", "Read"),
      named("t5", "Grep"),
    ];
    const rows = groupToolRuns(segs);
    const runs = rows.filter(isToolRun);
    expect(runs.map((r) => r.start)).toEqual([1, 4]);
    for (const run of runs)
      run.segs.forEach((seg, j) => {
        expect(seg).toBe(segs[run.start + j] as ToolSegment);
        expect(cardKey(7, seg, run.start + j)).toBe("tool:" + seg.id);
      });
    expect(flatten(rows)).toEqual(segs);
  });

  test("a null/empty list, and holes in it, come back empty", () => {
    expect(groupToolRuns(null)).toEqual([]);
    expect(groupToolRuns(undefined)).toEqual([]);
    expect(groupToolRuns([])).toEqual([]);
  });

  test("A HOLE BREAKS THE RUN AND MOVES NOTHING: index 2 is still index 2", () => {
    // The hole is dropped from the OUTPUT — there is nothing to paint for it —
    // but every index after it is the caller's own, because `cardsAfter`,
    // `cardKey` and `ToolRun.start` are all keyed by position in the list that
    // was handed in. Compacting the hole away would slide `start` to 1 and
    // re-key both chips.
    const segs = [
      named("t1", "Read"),
      null,
      named("t2", "Bash"),
      named("t3", "Grep"),
    ] as unknown as Segment[];
    const rows = groupToolRuns(segs);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toBe(segs[0]!);
    const run = rows[1]!;
    expect(isToolRun(run)).toBe(true);
    expect((run as { start: number }).start).toBe(2);
    expect((run as { segs: ToolSegment[] }).segs).toEqual([
      segs[2] as ToolSegment,
      segs[3] as ToolSegment,
    ]);
    // …and a card filed against the RAW index still lands on the right chip.
    const carded = groupToolRuns(segs, new Map<number, unknown>([[2, "card"]]));
    expect(carded).toEqual([segs[0]!, segs[2]!, segs[3]!]);
  });

  test("A STATUS THIS FILE HAS NO LITERAL FOR NEVER FOLDS", () => {
    // `ToolStatus` is "running" | "ok" | "error", and the fold asks for one of
    // the SETTLED two rather than for "not running". A payload with the field
    // missing — an older agent.py, a truncated row — is not known to be over,
    // and folding a stretch that might still be moving is the one mistake this
    // grouping cannot take back without shifting the transcript under a reader.
    const bare = (id: string): Segment =>
      ({ kind: "tool", id, name: "Read", input: {}, output: "", images: [] }) as unknown as Segment;
    const missing = [bare("t1"), bare("t2")];
    expect(groupToolRuns(missing)).toEqual(missing);
    const invented = [
      { ...(named("t1", "Read") as object), status: "queued" },
      { ...(named("t2", "Bash") as object), status: "queued" },
    ] as unknown as Segment[];
    expect(groupToolRuns(invented)).toEqual(invented);
    // The two real settled literals still fold.
    expect(groupToolRuns([named("t1", "Read"), named("t2", "Bash", "error")]).filter(isToolRun))
      .toHaveLength(1);
  });
});
