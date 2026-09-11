// WHERE THE RECAP POINTS, and the one rule that is easy to get wrong.
//
// `recapAnchor` is not "the last user turn's uuid": it is "the last user turn
// the TRANSCRIPT DRAWS WITH AN ANCHOR". The two came apart in the browser and
// the fold's body became a dead click — the reader had interrupted the reply
// before stepping away, the CLI wrote `[Request interrupted by user]` as a
// user-role record with a real uuid, `Turn` drew it as a centred note with no
// `data-msg`, and the anchor named a row that does not exist to scroll to.
// (Verified against the QA transcript: six user rows, five `data-msg` turns,
// and the recap's `for_uuid` was the sixth.)
import { expect, test } from "bun:test";

import { INTERRUPT_MARK } from "./wire";
import { isAnchorableTurn, recapAnchor } from "./recap";

const user = (uuid: string | null, text = "do the thing") => ({
  role: "user",
  key: uuid ?? "u:1",
  text,
  ...(uuid ? { uuid } : {}),
});
const bot = (i: number) => ({ role: "assistant", key: "h:" + i });

test("the anchor is the last user turn's uuid, past any number of replies", () => {
  expect(recapAnchor([user("a"), bot(1), user("b"), bot(2)])).toBe("b");
});

test("nothing to point at: empty, agent-only", () => {
  expect(recapAnchor([])).toBe(null);
  expect(recapAnchor([bot(0), bot(1)])).toBe(null);
});

test("A TRAILING INTERRUPT ROW IS SKIPPED — it is drawn with no `data-msg`", () => {
  const turns = [user("a"), bot(1), user("b"), bot(2), user("i", INTERRUPT_MARK)];
  // NOT "i" (the row the log renders as a note) and NOT `null` (there is a
  // perfectly good position behind it).
  expect(recapAnchor(turns)).toBe("b");
  // The CLI has shipped builds that append a newline to the record.
  expect(recapAnchor([user("a"), user("i", "  " + INTERRUPT_MARK + "\n")])).toBe("a");
  // Several in a row — an interrupted reply, resumed, interrupted again.
  expect(
    recapAnchor([user("a"), user("i1", INTERRUPT_MARK), bot(2), user("i2", INTERRUPT_MARK)]),
  ).toBe("a");
});

test("a transcript of nothing but interrupts has no position at all", () => {
  expect(recapAnchor([user("i1", INTERRUPT_MARK), bot(1), user("i2", INTERRUPT_MARK)])).toBe(null);
});

test("a prompt that TALKS about interrupts is still a prompt", () => {
  const said = "why did " + INTERRUPT_MARK + " appear in my log?";
  expect(recapAnchor([user("a"), user("b", said)])).toBe("b");
});

test("a live send with no uuid is `null`, not the turn before it", () => {
  // The endpoint requires `for_uuid`, so there is nothing to ask about yet —
  // and walking back to "a" would give the fold an anchor that a fresh send
  // does not change, which is what auto-dismisses it.
  expect(recapAnchor([user("a"), bot(1), user(null)])).toBe(null);
});

test("`isAnchorableTurn` is the one definition both halves read", () => {
  expect(isAnchorableTurn(user("a"))).toBe(true);
  expect(isAnchorableTurn(user("i", INTERRUPT_MARK))).toBe(false);
  expect(isAnchorableTurn(user(null))).toBe(false);
  expect(isAnchorableTurn(bot(0))).toBe(false);
  expect(isAnchorableTurn(undefined)).toBe(false);
});
