// THE ONE THING THAT MUST NOT BE A SPREAD (inventory 03 §D).
//
// Both send roads used to read `{ ...opts, ...takeAttachments() }`, which does
// not merge and fixes no order: the tray's `blocks` REPLACED the caller's, so
// PR3's `<annotations>` and PR4's `<live-app-state>` would have gone out missing
// with the run succeeding and nothing on screen to say so. These tests are the
// enforcement point that finding asked for — they fail if a future PR's block
// can be lost, or lands in front of one that has a stated place.
import { describe, expect, test } from "bun:test";

import {
  ANN_TAG,
  APP_STATE_TAG,
  BLOCK_ORDER,
  blockRank,
  composeBlocks,
  composeOutgoing,
  PANE_SHOT_TAG,
  stripBlocks,
} from "../protocol/wire";
import { mergeSendOptions, sendBlocks } from "./sendMerge";

const state = "<" + APP_STATE_TAG + ">\nrows\n</" + APP_STATE_TAG + ">";
const shots = "<" + PANE_SHOT_TAG + ">\n[]\n</" + PANE_SHOT_TAG + ">";
const notes = "<" + ANN_TAG + ">\nnote\n</" + ANN_TAG + ">";

describe("composeBlocks owns §D's order", () => {
  test("state, pane-shot, annotations — whatever order the owners arrive in", () => {
    // The three orders that matter: T's own, its reverse, and the one PR2's
    // spread produced by accident (the tray last).
    expect(composeBlocks([state], [shots], [notes])).toEqual([state, shots, notes]);
    expect(composeBlocks([notes], [shots], [state])).toEqual([state, shots, notes]);
    expect(composeBlocks([notes, state], [shots])).toEqual([state, shots, notes]);
  });

  test("NOTHING IS DROPPED — the whole point of the helper", () => {
    // The failure this replaces: `{ ...opts, ...mine }` kept exactly one of
    // these two lists.
    expect(composeBlocks([state, notes], [shots])).toHaveLength(3);
    // And an unrecognised block is kept too, at the end rather than in front of
    // the three with a stated place: an owner nobody has written yet is still an
    // owner whose words the user typed.
    const mine = "<something-new>x</something-new>";
    expect(composeBlocks([mine], [state])).toEqual([state, mine]);
  });

  test("empties and blanks fall out, so an empty tray adds no joiner", () => {
    expect(composeBlocks(null, undefined, [], ["", null, undefined])).toEqual([]);
    // Which is what keeps `composeOutgoing`'s "\n\n" from leading the message.
    expect(composeOutgoing("hi", composeBlocks(null, []))).toBe("hi");
  });

  test("the sort is STABLE: two blocks of one kind keep their owner's order", () => {
    const a = "<" + PANE_SHOT_TAG + ">\nA\n</" + PANE_SHOT_TAG + ">";
    const b = "<" + PANE_SHOT_TAG + ">\nB\n</" + PANE_SHOT_TAG + ">";
    expect(composeBlocks([a, b], [state])).toEqual([state, a, b]);
  });

  test("an opening tag with ATTRIBUTES still takes its own seat", () => {
    // The silent failure this pins: `<live-app-state v="2">` is PR4's tag to
    // write, and a bare `<tag>` sniff would have dropped it to the unranked tail
    // — state after the pictures, message still sent, nothing to say so.
    const versioned = "<" + APP_STATE_TAG + ' v="2">\nrows\n</' + APP_STATE_TAG + ">";
    expect(blockRank(versioned)).toBe(0);
    expect(composeBlocks([notes], [versioned])).toEqual([versioned, notes]);
    // A tag NAME that merely starts like one of the three is not one of them.
    expect(blockRank("<" + APP_STATE_TAG + "-draft>x</" + APP_STATE_TAG + "-draft>")).toBe(
      BLOCK_ORDER.length,
    );
    // And the sniff stops at the end of the tag it is reading: a `>` inside an
    // attribute is the attribute's, not the tag's end.
    expect(blockRank("<" + PANE_SHOT_TAG + ' alt="a>b">x</' + PANE_SHOT_TAG + ">")).toBe(1);
  });

  test("blockRank reads the opening tag, and only the opening tag", () => {
    expect(BLOCK_ORDER).toEqual([APP_STATE_TAG, PANE_SHOT_TAG, ANN_TAG]);
    expect(blockRank(state)).toBe(0);
    expect(blockRank(shots)).toBe(1);
    expect(blockRank(notes)).toBe(2);
    // Leading whitespace is not a different block.
    expect(blockRank("\n  " + notes)).toBe(2);
    // Plain prose has no tag and therefore no claim on a seat.
    expect(blockRank("just words")).toBe(BLOCK_ORDER.length);
  });

  test("the composed message still strips back to the typed text", () => {
    // The invariant PR3/PR4 inherit: reordering blocks must not break the
    // round trip the transcript and the receipts both read through.
    const out = composeOutgoing("please fix", composeBlocks([notes], [shots], [state]));
    expect(out.indexOf(state)).toBeLessThan(out.indexOf(shots));
    expect(out.indexOf(shots)).toBeLessThan(out.indexOf(notes));
    expect(stripBlocks(out)).toBe("please fix");
  });
});

describe("mergeSendOptions", () => {
  test("the three ADDITIVE fields are unioned, not overwritten", () => {
    const r1 = { kind: "image" as const, label: "a", view: "/a.png" };
    const r2 = { kind: "file" as const, label: "b", view: "/b.csv" };
    const out = mergeSendOptions(
      { blocks: [notes], readDirs: ["/notes"], attachments: [r1] },
      { blocks: [shots], readDirs: ["/shots"], attachments: [r2] },
    );
    expect(out.blocks).toEqual([shots, notes]);
    expect(out.readDirs).toEqual(["/notes", "/shots"]);
    expect(out.attachments).toEqual([r1, r2]);
  });

  test("a Read rule granted twice is granted once (T:11698)", () => {
    const out = mergeSendOptions({ readDirs: ["/shots", "/w"] }, { readDirs: ["/shots"] });
    expect(out.readDirs).toEqual(["/shots", "/w"]);
  });

  test("the scalars are the caller's, and the empty fields are absent", () => {
    // `model`/`effort`/`permission` are the composer's answer; the tray never
    // has one, so a merge must not blank them.
    const out = mergeSendOptions({ model: "opus", effort: "high" }, {});
    expect(out.model).toBe("opus");
    expect(out.effort).toBe("high");
    // Absent rather than `[]`: `sendFollowUp` refuses a send with no text and no
    // blocks by asking `blocks.length`, and an empty array on the wire would
    // also cost `read_dirs: "[]"` a place in every start.
    expect("blocks" in out).toBe(false);
    expect("readDirs" in out).toBe(false);
    expect("attachments" in out).toBe(false);
  });

  test("THE CALLER WINS A CONFLICTING SCALAR — the doc's rule, enforced", () => {
    // Latent only while `take()` returns no scalar at all: `{ ...base, ...extra }`
    // reads as a merge and gives EXTRA precedence, so the day the tray carries a
    // `model` of its own it would overrule the pill the user set.
    const out = mergeSendOptions(
      { model: "opus", effort: "high", permission: "prompt" },
      { model: "haiku", effort: "low", permission: "acceptEdits" },
    );
    expect(out.model).toBe("opus");
    expect(out.effort).toBe("high");
    expect(out.permission).toBe("prompt");
  });

  test("a scalar the caller merely left `undefined` is not an answer", () => {
    // The other half of the rule: "the caller's unless ONLY the tray named it".
    const out = mergeSendOptions({ model: undefined, effort: "high" }, { model: "haiku" });
    expect(out.model).toBe("haiku");
    expect(out.effort).toBe("high");
  });

  test("an empty tray leaves the caller's own lists exactly as they were", () => {
    const out = mergeSendOptions({ blocks: [state], readDirs: ["/w"] }, {});
    expect(out.blocks).toEqual([state]);
    expect(out.readDirs).toEqual(["/w"]);
  });
});

describe("sendBlocks ADDS the send path's block to the caller's", () => {
  test("a caller's block and ours both go out, in BLOCK_ORDER", () => {
    // The send path used to build its base as `{ ...opts, blocks: [ours] }` —
    // a replacement, one line above the merge that exists to prevent one. The
    // caller's `<live-app-state>` was dropped with the run succeeding
    // (whole-stack review, PR #1074).
    const out = sendBlocks([state], notes);
    expect(out).toEqual([state, notes]);
  });

  test("the whole send: a caller's block, ours, and the TRAY's — all three, ordered", () => {
    // The shape `beginSend` composes: `opts.blocks` from the caller, the
    // `<annotations>` this send stamped, and the pictures the tray took out of
    // itself. Every tag on the wire, in the order §D states.
    const merged = mergeSendOptions(
      { model: "opus", blocks: sendBlocks([state], notes) },
      { blocks: [shots] },
    );
    expect(merged.blocks).toEqual([state, shots, notes]);
    expect(merged.blocks!.map(blockRank)).toEqual([0, 1, 2]);
    expect(merged.blocks).toHaveLength(BLOCK_ORDER.length);
    // And it reaches the wire that way, the typed words last.
    const wire = composeOutgoing("have a look", merged.blocks);
    expect(wire.indexOf(APP_STATE_TAG)).toBeLessThan(wire.indexOf(PANE_SHOT_TAG));
    expect(wire.indexOf(PANE_SHOT_TAG)).toBeLessThan(wire.indexOf(ANN_TAG));
    expect(stripBlocks(wire)).toBe("have a look");
  });

  test("no caller block, no block of ours — nothing invented", () => {
    expect(sendBlocks(undefined)).toEqual([]);
    expect(sendBlocks(undefined, null)).toEqual([]);
    // A wordless send that carries only notes is still just the notes.
    expect(sendBlocks(undefined, notes)).toEqual([notes]);
    // ...and a caller with blocks and NO notes keeps every one of them.
    expect(sendBlocks([state, shots], null)).toEqual([state, shots]);
  });
});
