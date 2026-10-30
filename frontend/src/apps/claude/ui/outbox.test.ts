import { expect, test } from "bun:test";
import {
  heldKey, loadHeld, outboxHint, popNewest, pushBack, pushFront, pushFrontAll, saveHeld, shiftOldest,
  shiftOldestSendable, takeById,
} from "./outbox";

const e = (id: string, text = id) => ({ id, text, payload: null });

test("a stop hands N lines back as N not-sent entries, order kept, ahead of the rest", () => {
  // Claude read neither "C" nor "D" before the stop; both come back as bubbles
  // the reader can pull, never into the box, and never drained on their own.
  const later = pushBack([], e("later"));
  const list = pushFrontAll(later, [{ ...e("C"), notSent: true }, { ...e("D"), notSent: true }]);
  expect(list.map((x) => x.id)).toEqual(["C", "D", "later"]);
  expect(list.filter((x) => x.notSent).map((x) => x.id)).toEqual(["C", "D"]);
  // The drain skips both and takes the one sendable line.
  expect(shiftOldestSendable(list).entry?.id).toBe("later");
  // ↑ still pulls the newest of everything, not-sent included.
  expect(popNewest(pushFrontAll([], [{ ...e("C"), notSent: true }])).entry?.id).toBe("C");
});

test("the drain steps over a not-sent line and keeps the order", () => {
  const list = pushBack(pushBack(pushBack([], { ...e("a"), notSent: true }), e("b")), e("c"));
  const first = shiftOldestSendable(list);
  expect(first.entry?.id).toBe("b");
  expect(first.rest.map((x) => x.id)).toEqual(["a", "c"]);
  const second = shiftOldestSendable(first.rest);
  expect(second.entry?.id).toBe("c");
  expect(second.rest.map((x) => x.id)).toEqual(["a"]);
  // Only not-sent lines left: nothing to drain, list untouched.
  expect(shiftOldestSendable(second.rest).entry).toBeNull();
  expect(shiftOldestSendable(second.rest).rest.map((x) => x.id)).toEqual(["a"]);
});

test("the drain takes the OLDEST line first, in the order typed", () => {
  let list = pushBack([], e("a"));
  list = pushBack(list, e("b"));
  list = pushBack(list, e("c"));
  const first = shiftOldest(list);
  expect(first.entry?.id).toBe("a");
  const second = shiftOldest(first.rest);
  expect(second.entry?.id).toBe("b");
  expect(shiftOldest(second.rest).entry?.id).toBe("c");
  expect(shiftOldest([]).entry).toBeNull();
});

test("↑ pulls back the NEWEST line, up again for an older one", () => {
  const list = pushBack(pushBack([], e("a")), e("b"));
  const back = popNewest(list);
  expect(back.entry?.id).toBe("b");
  expect(popNewest(back.rest).entry?.id).toBe("a");
  expect(popNewest([]).entry).toBeNull();
});

test("a handed-back line goes AHEAD of everything typed since it", () => {
  // The reader typed "b" while "a" was out; "a" came back unsent. It was said
  // first, so it goes first — and never into the box "b" is being typed in.
  const list = pushFront(pushBack([], e("b")), { ...e("a"), notSent: true });
  expect(list.map((x) => x.id)).toEqual(["a", "b"]);
  expect(shiftOldest(list).entry?.notSent).toBe(true);
});

test("every move returns a new array — the mirrored state must repaint", () => {
  const list = pushBack([], e("a"));
  expect(pushBack(list, e("b"))).not.toBe(list);
  expect(shiftOldest(list).rest).not.toBe(list);
  expect(popNewest(list).rest).not.toBe(list);
  expect(list).toHaveLength(1);
});

test("a click takes ONE entry by id and keeps the others in order", () => {
  const list = pushBack(pushBack(pushBack([], e("a")), e("b")), e("c"));
  const got = takeById(list, "b");
  expect(got.entry?.id).toBe("b");
  expect(got.rest.map((x) => x.id)).toEqual(["a", "c"]);
  expect(takeById(list, "zz").entry).toBeNull();
});

test("the hint counts only lines that WILL send; not-sent rows get their own line", () => {
  expect(outboxHint(0)).toBe("");
  expect(outboxHint(1)).toContain("1 message waiting");
  expect(outboxHint(3)).toContain("3 messages waiting");
  expect(outboxHint(1)).toContain("↑");
  // Not-sent rows never drain, so they are never "waiting to send".
  expect(outboxHint(0, 2)).toBe("2 messages not sent · click one to edit");
  expect(outboxHint(0, 1)).toContain("1 message not sent");
  // Sendable lines win the one line there is.
  expect(outboxHint(1, 2)).toContain("waiting");
});

test("lines held through a turn drain FIFO, one per call", () => {
  let list = ["A", "B", "C"].reduce((l, id) => pushBack(l, e(id)), [] as ReturnType<typeof e>[]);
  const out: string[] = [];
  for (;;) {
    const { entry, rest } = shiftOldestSendable(list);
    if (!entry) break;
    out.push(entry.id);
    expect(rest).toHaveLength(list.length - 1);
    list = rest;
  }
  expect(out).toEqual(["A", "B", "C"]);
});

function fakeStore() {
  const m = new Map<string, string>();
  return {
    m,
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
}

test("held lines are saved per session and read back in order", () => {
  const store = fakeStore();
  saveHeld("s1", [{ text: "A", items: [] }, { text: "B", items: [{ view: "/p.png" }] }], store);
  saveHeld("s2", [{ text: "other", items: [] }], store);
  expect(store.m.has(heldKey("s1"))).toBe(true);
  expect(loadHeld("s1", store)).toEqual([
    { text: "A", items: [] },
    { text: "B", items: [{ view: "/p.png" }] },
  ]);
  expect(loadHeld("s2", store).map((l) => l.text)).toEqual(["other"]);
  expect(loadHeld("s3", store)).toEqual([]);
});

test("an empty list clears the session's key; no session id touches nothing", () => {
  const store = fakeStore();
  saveHeld("s1", [{ text: "A", items: [] }], store);
  saveHeld("s1", [], store);
  expect(store.m.size).toBe(0);
  saveHeld("", [{ text: "A", items: [] }], store);
  expect(store.m.size).toBe(0);
  expect(loadHeld("", store)).toEqual([]);
});

test("unreadable or malformed storage reads as nothing held", () => {
  const store = fakeStore();
  store.m.set(heldKey("s1"), "{not json");
  expect(loadHeld("s1", store)).toEqual([]);
  store.m.set(heldKey("s1"), JSON.stringify({ text: "A" }));
  expect(loadHeld("s1", store)).toEqual([]);
  store.m.set(heldKey("s1"), JSON.stringify([{ text: 3 }, { text: "" }, { text: "ok" }]));
  expect(loadHeld("s1", store)).toEqual([{ text: "ok", items: [] }]);
  expect(loadHeld("s1", null)).toEqual([]);
});

test("a picture-only line is kept; a line with neither words nor pictures is not", () => {
  const store = fakeStore();
  store.m.set(heldKey("s1"), JSON.stringify([{ text: "", items: [{ view: "/a.png" }] }, { text: "", items: [] }]));
  expect(loadHeld("s1", store)).toEqual([{ text: "", items: [{ view: "/a.png" }] }]);
});
