import { expect, test } from "bun:test";
import {
  outboxHint, popNewest, pushBack, pushFront, pushFrontAll, shiftOldest, shiftOldestSendable,
  takeById,
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

test("the pending journal: save, load and split round-trip per session", async () => {
  const { savePending, loadPending, splitPending } = await import("./outbox");
  const mem = new Map<string, string>();
  (globalThis as any).sessionStorage = {
    getItem: (k: string) => mem.get(k) ?? null,
    setItem: (k: string, v: string) => void mem.set(k, v),
    removeItem: (k: string) => void mem.delete(k),
  };
  savePending("s1", [{ id: "a", text: "one" }, { id: "b", text: "two" }]);
  expect(loadPending("s1").length).toBe(2);
  expect(loadPending("s2")).toEqual([]);
  // Matched by id, never by text: "b" is on the server, "a" is not.
  const { resend, drop } = splitPending(loadPending("s1"), new Set(["b"]));
  expect(resend.map((l) => l.id)).toEqual(["a"]);
  expect(drop.map((l) => l.id)).toEqual(["b"]);
  savePending("s1", []);
  expect(loadPending("s1")).toEqual([]);
});
