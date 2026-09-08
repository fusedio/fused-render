// The list, the param it survives in, and the four ways notes leave it.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { createMemoryParamsStore } = await import("../params/store");
const { ANN_MODE_PARAM, ANN_PARAM, createAnnStore, parseAnnotations, serializeAnnotations } =
  await import("./store");
import type { Annotation } from "./types";

function fixed(start = 1000) {
  let t = start;
  return () => t++;
}

function make(initial: Record<string, string> = {}, hosted = false) {
  const params = createMemoryParamsStore(initial);
  let n = 0;
  const store = createAnnStore({ params, hosted, now: fixed(), newId: () => "id" + ++n });
  return { params, store };
}

describe("the param's shape is T's, verbatim (T:6600, 6612)", () => {
  test("a round trip through the param keeps every field and the key order", () => {
    // The exact shape T saves: `{id, content, createdAt, ...anchor}` for an
    // element note, and `kind:"point"` with page x/y for a spot.
    const written: Annotation[] = [
      {
        id: "a",
        content: "tighten this",
        createdAt: 1720000000000,
        anchorId: "hero",
        tag: "h1",
        text: "Welcome",
        iu: 0.5,
        iv: 0.25,
        sent: 0,
      },
      {
        id: "b",
        content: "",
        createdAt: 1720000000001,
        kind: "point",
        x: 120,
        y: 340,
        nearPath: "div:nth-of-type(1)>p:nth-of-type(2)",
        t: 4.2,
      },
    ];
    const json = serializeAnnotations(written);
    expect(json).toBe(JSON.stringify(written));
    expect(parseAnnotations(json)).toEqual(written);
  });

  test("a malformed param means NO notes, never a broken chat", () => {
    expect(parseAnnotations("{oops")).toEqual([]);
    expect(parseAnnotations("{}")).toEqual([]);
    expect(parseAnnotations(undefined)).toEqual([]);
    expect(parseAnnotations("[null, 3, {\"id\":\"a\"}]")).toEqual([
      { id: "a" } as unknown as Annotation,
    ]);
  });

  test("every write lands in the param and notifies once", () => {
    const { params, store } = make();
    const seen: number[] = [];
    store.subscribe((l) => seen.push(l.length));
    store.add({ content: "one", anchorId: "x" });
    expect(seen).toEqual([1]);
    expect(JSON.parse(params.get(ANN_PARAM) as string)).toEqual([
      { content: "one", anchorId: "x", id: "id1", createdAt: 1000 },
    ]);
  });

  test("hydrated from the param on boot — split", () => {
    const seed = JSON.stringify([{ id: "a", content: "hi", createdAt: 1 }]);
    const { store } = make({ [ANN_PARAM]: seed });
    expect(store.list()).toHaveLength(1);
  });

  test("HOSTED boots empty (T:6606): a sidebar teardown is a mode switch as often as a reload", () => {
    const seed = JSON.stringify([{ id: "a", content: "hi", createdAt: 1 }]);
    const { store } = make({ [ANN_PARAM]: seed }, true);
    expect(store.list()).toEqual([]);
  });
});

describe("editing and removing", () => {
  test("edit changes only a PENDING note's words", () => {
    const { store } = make();
    const a = store.add({ content: "one", anchorId: "x" });
    store.edit(a.id, "two");
    expect(store.list()[0].content).toBe("two");
    store.markSent([store.list()[0]]);
    store.edit(a.id, "three");
    expect(store.list()[0].content).toBe("two"); // sent is Claude's already
  });

  test("remove is a no-op for an id that is not there", () => {
    const { store } = make();
    let hits = 0;
    store.subscribe(() => hits++);
    store.remove("nope");
    expect(hits).toBe(0);
  });

  test("clear empties the list (enterNoPane step 1a)", () => {
    const { store } = make();
    store.add({ content: "one" });
    store.clear();
    expect(store.list()).toEqual([]);
  });
});

describe("the round, and Esc's discard (T:6580, 8401, PR #1028)", () => {
  test("only THIS round's unsent notes go; earlier rounds and sent ones stay", () => {
    const { store } = make();
    store.startRound(1000);
    const old = store.add({ content: "last round", createdAt: 500 });
    const sent = store.add({ content: "already sent", createdAt: 1500, sent: 1 });
    const mine = store.add({ content: "this round", createdAt: 1500 });
    expect(store.discardRound()).toBe(true);
    const ids = store.list().map((c) => c.id);
    expect(ids).toEqual([old.id, sent.id]);
    expect(ids).not.toContain(mine.id);
  });

  test("nothing to throw is reported as such — no write, no history entry", () => {
    const { store } = make();
    store.startRound(1000);
    store.add({ content: "old", createdAt: 1 });
    expect(store.discardRound()).toBe(false);
  });

  test("startRound stamps `now` when not told otherwise", () => {
    const { store } = make();
    store.startRound();
    expect(store.roundStart()).toBe(1000);
  });
});

describe("resolveSent (T:10608)", () => {
  test("a clean run drops everything stamped sent, and only that", () => {
    const { params, store } = make();
    store.add({ content: "kept" });
    const b = store.add({ content: "gone" });
    store.markSent([b]);
    expect(store.resolveSent()).toBe(true);
    expect(store.list().map((c) => c.content)).toEqual(["kept"]);
    expect(JSON.parse(params.get(ANN_PARAM) as string)).toHaveLength(1);
  });

  test("nothing sent, nothing to resolve — and no write", () => {
    const { store } = make();
    store.add({ content: "kept" });
    let hits = 0;
    store.subscribe(() => hits++);
    expect(store.resolveSent()).toBe(false);
    expect(hits).toBe(0);
  });
});

describe("the send's three writes (T:16053, 16065, 16086)", () => {
  test("labels come off the index in the WHOLE list, not in `pending`", () => {
    const { store } = make();
    const a = store.add({ content: "a" });
    store.markSent([a]); // index 0 is a sent note now
    const b = store.add({ content: "b" });
    const stamped = store.stampLabels([b]);
    expect(stamped.map((c) => c.label)).toEqual(["B"]);
    expect(store.list()[1].label).toBe("B");
  });

  test("markSent stamps 1, the NUMBER the param holds", () => {
    const { params, store } = make();
    const a = store.add({ content: "a" });
    store.markSent([a]);
    expect(JSON.parse(params.get(ANN_PARAM) as string)[0].sent).toBe(1);
  });

  test("a failed send RE-APPENDS notes resolveSent has already dropped", () => {
    const { store } = make();
    const a = store.add({ content: "a" });
    const b = store.add({ content: "b" });
    const pending = [a, b];
    store.markSent(pending);
    store.resolveSent(); // the session ended; the sent notes are gone from the list
    expect(store.list()).toEqual([]);
    store.unmarkSent(pending);
    expect(store.list().map((c) => [c.content, c.sent])).toEqual([
      ["a", 0],
      ["b", 0],
    ]);
  });

  test("…and only un-marks the ones still there when they still are", () => {
    const { store } = make();
    const a = store.add({ content: "a" });
    store.markSent([a]);
    store.unmarkSent([store.list()[0]]);
    expect(store.list()).toHaveLength(1);
    expect(store.list()[0].sent).toBe(0);
  });
});

describe("merge (the overview's write half)", () => {
  test("replaces by id, keeps list order, one notification", () => {
    const { store } = make();
    const a = store.add({ content: "a" });
    const b = store.add({ content: "b" });
    let hits = 0;
    store.subscribe(() => hits++);
    store.merge([
      { ...b, offscreen: "why" },
      { ...a, label: "A" },
    ]);
    expect(hits).toBe(1);
    expect(store.list().map((c) => c.content)).toEqual(["a", "b"]);
    expect(store.list()[1].offscreen).toBe("why");
    expect(store.list()[0].label).toBe("A");
  });

  test("an id that is gone is not resurrected", () => {
    const { store } = make();
    store.merge([{ id: "ghost", content: "", createdAt: 0 }]);
    expect(store.list()).toEqual([]);
  });
});

describe("annmode: the write is skipped when the URL already MEANS this (T:7639, RH/PR)", () => {
  test('absent → "0" writes nothing: a semantic no-op would cost a history entry', () => {
    const { params, store } = make();
    store.syncModeParam("0");
    expect(params.get(ANN_MODE_PARAM)).toBeUndefined();
  });
  test('absent → "1" writes: arming is a state change the user asked for', () => {
    const { params, store } = make();
    store.syncModeParam("1");
    expect(params.get(ANN_MODE_PARAM)).toBe("1");
  });
  test('"1" → "1" is silent; "1" → "2" writes (the mode CHANGED)', () => {
    const { params, store } = make({ [ANN_MODE_PARAM]: "1" });
    store.syncModeParam("1");
    expect(params.get(ANN_MODE_PARAM)).toBe("1");
    store.syncModeParam("2");
    expect(params.get(ANN_MODE_PARAM)).toBe("2");
  });
  test('anything that is not "1"/"2" reads as off, so "0" over junk still writes', () => {
    const { params, store } = make({ [ANN_MODE_PARAM]: "banana" });
    store.syncModeParam("0");
    expect(params.get(ANN_MODE_PARAM)).toBe("banana"); // both mean off
    store.syncModeParam("1");
    expect(params.get(ANN_MODE_PARAM)).toBe("1");
  });
});
