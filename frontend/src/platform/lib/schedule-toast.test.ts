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
