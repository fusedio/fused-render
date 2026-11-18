// THE QUEUED STRIP'S ROWS: lines typed while a turn runs, until the CLI echoes
// them. Keyed by line id, never by words.
import { describe, expect, it } from "bun:test";
import { queuedLines } from "./queued-lines";
import type { Turn } from "./controller-api";

const user = (key: string, text: string, extra: Record<string, unknown> = {}): Turn =>
  ({ role: "user", key, text, ...extra }) as Turn;

describe("queuedLines", () => {
  it("lifts a controller-queued bubble out of the transcript into the strip", () => {
    const turns = [user("u1", "hello"), user("u2", "more")];
    const out = queuedLines({ turns, queuedKeys: ["u2"], inbox: [] });
    expect(out.lines.map((l) => [l.id, l.text])).toEqual([["u2", "more"]]);
    expect(out.hiddenKeys.has("u2")).toBe(true);
    expect(out.hiddenKeys.has("u1")).toBe(false);
  });

  it("an outbox-parked or held line is a strip row that can be pulled back", () => {
    const turns = [user("a", "A", { pending: "queued" }), user("b", "B", { pending: "held" })];
    const out = queuedLines({ turns, queuedKeys: [], inbox: [] });
    expect(out.lines.map((l) => [l.id, l.pending])).toEqual([
      ["a", "queued"],
      ["b", "held"],
    ]);
  });

  it("a follow-up on its way to the run is a strip row from the keypress, and the SAME row once it lands", () => {
    // "sending": typed into a live run, the controller does not have it yet.
    const typed = [user("u1", "hello"), user("f", "and also this", { pending: "sending" })];
    const before = queuedLines({ turns: typed, queuedKeys: [], inbox: [] });
    expect(before.lines.map((l) => [l.id, l.text, l.pending])).toEqual([["f", "and also this", "sending"]]);
    expect(before.hiddenKeys.has("f")).toBe(true);
    // The send's own bubble adopts the row (the tag comes off) and the
    // controller names it as queued: one id, so the strip never remounts it.
    const landed = [user("u1", "hello"), user("f", "and also this")];
    const after = queuedLines({ turns: landed, queuedKeys: ["f"], inbox: [] });
    expect(after.lines.map((l) => [l.id, l.pending])).toEqual([["f", undefined]]);
  });

  it("a not-sent line stays in the transcript", () => {
    const turns = [user("n", "oops", { pending: "notSent" })];
    const out = queuedLines({ turns, queuedKeys: [], inbox: [] });
    expect(out.lines).toEqual([]);
    expect(out.hiddenKeys.size).toBe(0);
  });

  it("after a reload the server inbox alone fills the strip", () => {
    const out = queuedLines({
      turns: [user("u1", "hello")],
      queuedKeys: [],
     
      inbox: [
        { id: "f1", uuid: "x1", text: "first" },
        { id: "f2", uuid: "x2", text: "second" },
      ],
    });
    expect(out.lines.map((l) => l.text)).toEqual(["first", "second"]);
  });

  it("two identical lines are two rows", () => {
    const out = queuedLines({
      turns: [],
      queuedKeys: [],
     
      inbox: [
        { id: "f1", uuid: "x1", text: "again" },
        { id: "f2", uuid: "x2", text: "again" },
      ],
    });
    expect(out.lines.map((l) => l.id)).toEqual(["f1", "f2"]);
  });

  it("an echoed line (a transcript bubble, no longer queued) is not a strip row", () => {
    const out = queuedLines({
      turns: [user("u1", "hello")],
      queuedKeys: [],
     
      inbox: [],
    });
    expect(out.lines).toEqual([]);
  });
});

describe("queuedLines and identical words", () => {
  it("an earlier echoed line with the same words does not retire a queued uuid entry", () => {
    const out = queuedLines({
      turns: [user("u1", "again")],
      queuedKeys: [],
      inbox: [{ id: "f2", uuid: "x2", text: "again" }],
    });
    expect(out.lines.map((l) => l.id)).toEqual(["f2"]);
  });

  it("the message that OPENED the turn is in the transcript, not in the queue under it", () => {
    // The run lists the opening message in its inbox until the CLI echoes it;
    // the page is already drawing that very message as a bubble.
    const out = queuedLines({
      turns: [user("u1", "hello", { clientId: "x1" })],
      queuedKeys: [],
      inbox: [{ id: "f1", uuid: "x1", text: "hello" }],
    });
    expect(out.lines).toEqual([]);
    expect(out.hiddenKeys.size).toBe(0);
    // BY ID, never by words: another message saying the same thing is queued.
    const two = queuedLines({
      turns: [user("u1", "hello", { clientId: "x1" })],
      queuedKeys: [],
      inbox: [
        { id: "f1", uuid: "x1", text: "hello" },
        { id: "f2", uuid: "x2", text: "hello" },
      ],
    });
    expect(two.lines.map((l) => l.id)).toEqual(["f2"]);
  });

  it("an inbox row for a line already in the strip is not drawn twice", () => {
    const out = queuedLines({
      turns: [user("u2", "again")],
      queuedKeys: ["u2"],
      inbox: [{ id: "f2", uuid: "x2", text: "again" }],
    });
    expect(out.lines.map((l) => l.id)).toEqual(["u2"]);
  });

  it("two identical queued lines, one in the strip and one only in the inbox, stay two", () => {
    const out = queuedLines({
      turns: [user("u2", "again")],
      queuedKeys: ["u2"],
      inbox: [
        { id: "f1", uuid: "x1", text: "again" },
        { id: "f2", uuid: "x2", text: "again" },
      ],
    });
    expect(out.lines).toHaveLength(2);
  });
});
