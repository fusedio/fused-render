// The scheduled-message toast rules (schedule-toast.ts). The polling hook that
// consumes them (scheduleEvents.ts) is not tested here — the decision table is
// the part with rules in it, which is the split server-status.ts uses.
import { describe, expect, it } from "bun:test";
import { eventLabel, toastForEvent } from "./schedule-toast";
import type { ScheduleEvent } from "./api";

function ev(over: Partial<ScheduleEvent> = {}): ScheduleEvent {
  return {
    id: 1,
    kind: "done",
    entry_id: "e1",
    target: "/Users/x/proj",
    message: "update the changelog",
    detail: "",
    ts: 0,
    ...over,
  };
}

describe("toastForEvent", () => {
  it("says a message ran, as an info that goes away on its own", () => {
    const t = toastForEvent(ev({ kind: "done" }));
    expect(t.tone).toBe("info");
    // nothing to decide, so it must not demand to be dismissed
    expect(t.needsAttention).toBe(false);
    expect(t.msg).toBe("Scheduled message ran: update the changelog");
  });

  it("treats a failure as needing a person", () => {
    const t = toastForEvent(ev({ kind: "failed" }));
    expect(t.tone).toBe("error");
    expect(t.needsAttention).toBe(true);
    expect(t.msg).toContain("failed");
  });

  it("distinguishes missed from failed in the wording", () => {
    // Nothing went wrong — the app just wasn't running inside the catch-up
    // window — so calling it "failed" would misdescribe it. It still needs a
    // person: the user asked for something that did not happen.
    const t = toastForEvent(ev({ kind: "missed" }));
    expect(t.msg).toContain("was missed");
    expect(t.msg).not.toContain("failed");
    expect(t.needsAttention).toBe(true);
  });

  it("asks for an answer on a run that has parked on a card", () => {
    // The one kind here about a run that has NOT ended. Info, because nothing
    // has gone wrong — the run is doing exactly what it should, which is
    // refusing to act without an answer — and persistent all the same, because
    // the ask does not expire and only a person can end it. That pairing is why
    // `needsAttention` is a field of its own and not `tone === "error"`.
    const t = toastForEvent(ev({
      kind: "attention",
      message: "clean the build tree",
      session_id: "sess-7",
      detail: "Bash · rm -rf build",
    }));
    expect(t.tone).toBe("info");
    expect(t.needsAttention).toBe(true);
    expect(t.msg).toBe("Task needs your input: clean the build tree");
    // ...and it points at the CHAT, not at the page that lists it: the card is
    // in the thread, and /tasks is one more click away from answering it.
    expect(t.open).toEqual({ target: "/Users/x/proj", sessionId: "sess-7" });
  });

  it("names no conversation for the kinds whose news is a row", () => {
    // A failed or missed run is OVER — the row on /tasks carries the reason, the
    // target and the run id, which is more than the thread would say — so these
    // keep the page as their action and `open` stays null.
    for (const kind of ["done", "failed", "missed"] as const) {
      expect(toastForEvent(ev({ kind })).open).toBe(null);
    }
    // An older server sends no session id: the toast still opens, and the caller
    // falls back to /tasks (scheduleEvents).
    expect(toastForEvent(ev({ kind: "attention" })).open)
      .toEqual({ target: "/Users/x/proj", sessionId: "" });
  });

  it("identifies the message by what the user typed", () => {
    // A toast saying only "a scheduled message failed" sends the user hunting.
    expect(toastForEvent(ev({ kind: "failed", message: "deploy the docs" })).msg)
      .toContain("deploy the docs");
  });
});

describe("eventLabel", () => {
  it("takes the prompt's first line", () => {
    expect(eventLabel(ev({ message: "first line\nsecond line" }))).toBe("first line");
  });

  it("clips a long prompt rather than wrapping a paragraph into the column", () => {
    const label = eventLabel(ev({ message: "x".repeat(200) }));
    expect(label.length).toBeLessThanOrEqual(60);
    expect(label.endsWith("…")).toBe(true);
  });

  it("falls back when there is nothing to show", () => {
    expect(eventLabel(ev({ message: "" }))).toBe("Scheduled message");
    expect(eventLabel(ev({ message: "   \n  " }))).toBe("Scheduled message");
  });
});
