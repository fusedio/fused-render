// WHICH UNDRAINED FOLLOW-UPS STILL NEED A BUBBLE (`protocol/inbox`).
//
// The message is in the CLI's own stdin queue; the page's optimistic bubble is
// the only other copy and does not survive a reload or a `refreshHistory`. The
// run reports the list, and this decides which of its entries nothing else on
// screen is already saying.
import { describe, expect, it } from "bun:test";
import { inboxBubbles, inboxKey } from "./inbox";

const msg = (id: string, text: string) => ({ id, text });

describe("the rows a held follow-up earns", () => {
  it("draws every entry the run is holding, in the order it took them", () => {
    const rows = inboxBubbles([msg("f1", "first"), msg("f2", "second")], [], []);
    expect(rows.map((r) => r.text)).toEqual(["first", "second"]);
    expect(rows.map((r) => r.id)).toEqual(["f1", "f2"]);
  });

  it("draws nothing at all for an empty, absent or older-server answer", () => {
    // An agent.py without the field sends none, which is the same as an empty
    // inbox — and is exactly what this pane did before the field existed.
    expect(inboxBubbles([], ["typed"], [])).toEqual([]);
    expect(inboxBubbles(undefined, ["typed"], [])).toEqual([]);
    expect(inboxBubbles(null, [], [])).toEqual([]);
  });

  it("DEDUPES against the optimistic bubble this page already put up", () => {
    // For one poll lap both are true: the reader's own bubble is on screen and
    // the run is reporting the same words. Two bubbles for one message is the
    // other way to get this wrong.
    const rows = inboxBubbles([msg("f1", "hello"), msg("f2", "again")], ["hello"], []);
    expect(rows.map((r) => r.text)).toEqual(["again"]);
  });

  it("matches on TEXT as well as id, because the optimistic row has no id", () => {
    // The bubble is minted on the keystroke, before anything has answered, so
    // the only thing the two copies share is the words. Whitespace only, because
    // a follow-up's wire form can gain a trailing newline on the way through
    // stdin — and nothing looser, because two messages differing in case are two
    // messages.
    expect(inboxKey("  hello\n")).toBe("hello");
    expect(inboxBubbles([msg("f1", "hello\n")], ["hello"], [])).toEqual([]);
    expect(inboxBubbles([msg("f1", "Hello")], ["hello"], [])).toHaveLength(1);
  });

  it("DROPS an entry the moment the transcript gains its row", () => {
    // The model got to it: there is a real user turn saying exactly this, and a
    // second copy under the log would be the message appearing twice.
    expect(inboxBubbles([msg("f1", "answer me")], [], ["earlier", "answer me"])).toEqual([]);
    // …and the ones it has NOT reached are still drawn.
    const rows = inboxBubbles([msg("f1", "done"), msg("f2", "pending")], [], ["done"]);
    expect(rows.map((r) => r.text)).toEqual(["pending"]);
  });

  it("DROPS an entry that simply left the inbox — the list IS the state", () => {
    expect(inboxBubbles([msg("f1", "a"), msg("f2", "b")], [], [])).toHaveLength(2);
    expect(inboxBubbles([msg("f2", "b")], [], [])).toHaveLength(1);
    expect(inboxBubbles([], [], [])).toHaveLength(0);
  });

  it("draws TWO bubbles for two entries saying the same words", () => {
    // The host's list is a list of MESSAGES, not a set of strings: a reader who
    // sends "go on" twice while a turn runs has sent two follow-ups, both are
    // held, and folding them into one bubble showed the second going missing
    // (🔴 review 2026-09-12). The dedupe inside the inbox is by ID.
    const rows = inboxBubbles([msg("f1", "go on"), msg("f2", "go on")], [], []);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.id)).toEqual(["f1", "f2"]);
    expect(rows.map((r) => r.text)).toEqual(["go on", "go on"]);
    // …and ONE entry listed twice in one payload is still one bubble.
    expect(inboxBubbles([msg("f1", "go on"), msg("f1", "go on")], [], [])).toHaveLength(1);
  });

  it("spends an already-drawn text ONCE, not against every row saying it", () => {
    // Two identical follow-ups with one optimistic bubble up is ONE bubble here
    // — the other copy is the one on screen — rather than none.
    const rows = inboxBubbles([msg("f1", "go on"), msg("f2", "go on")], ["go on"], []);
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("f2");
    // Both spoken for: nothing is drawn twice.
    expect(inboxBubbles([msg("f1", "go on"), msg("f2", "go on")], ["go on"], ["go on"]))
      .toHaveLength(0);
  });

  it("draws a DRAINED entry exactly as it draws a held one", () => {
    // The host has taken it off the pile and the transcript has not echoed it
    // back; for the reader that is the same fact, and a bubble that blinked out
    // at the drain and back in at the echo would be the app narrating its own
    // plumbing. The ECHO is what retires it — a real user turn — and nothing
    // else.
    const rows = inboxBubbles(
      [{ id: "f1", text: "go on", drained: true }, msg("f2", "and again")],
      [],
      [],
    );
    expect(rows.map((r) => r.text)).toEqual(["go on", "and again"]);
    // …and once the turn says it, the drained row goes with the rest.
    expect(
      inboxBubbles([{ id: "f1", text: "go on", drained: true }], [], ["go on"]),
    ).toEqual([]);
  });

  it("retires a duplicated ID even when the row was already drawn elsewhere", () => {
    // Bugbot PR #1124. The id dedupe skipped the entry whose words are already
    // on screen, so a host listing the same entry twice in one payload spent the
    // optimistic copy on the first and drew a bubble for the second — the
    // duplicate this dedupe exists to stop, reached by the one path that did not
    // mark the id as seen.
    expect(inboxBubbles([msg("f1", "go on"), msg("f1", "go on")], ["go on"], []))
      .toEqual([]);
    // …and two DIFFERENT entries saying the same words are still two messages.
    expect(
      inboxBubbles([msg("f1", "go on"), msg("f2", "go on")], ["go on"], []).map((r) => r.id),
    ).toEqual(["f2"]);
  });

  it("draws no bubble for a wordless entry", () => {
    // Pictures alone have no typed line, and an empty bubble under the log says
    // nothing a reader can read.
    expect(inboxBubbles([msg("f1", ""), msg("f2", "   ")], [], [])).toEqual([]);
  });

  it("falls back to the text as its key when the server named no id", () => {
    // The key only has to be stable across polls and unique in the list, which
    // the words are when the ids are missing.
    expect(inboxBubbles([{ id: "", text: "unnamed" }], [], [])[0].id).toBe("unnamed");
    // …and a second id-less row saying the same words takes a suffix rather than
    // colliding: React keys have to be unique, and these are two messages.
    expect(
      inboxBubbles([{ id: "", text: "unnamed" }, { id: "", text: "unnamed" }], [], []).map(
        (r) => r.id,
      ),
    ).toEqual(["unnamed", "unnamed#2"]);
  });
});
