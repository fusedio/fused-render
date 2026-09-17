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
  it("reports a run that finished successfully as a suppressible info toast", () => {
    // SPEC-quiet-notifications.md §5 reverses the old "done is not news"
    // rule: it IS news when nobody was looking, so it now produces a toast —
    // but one that suppresses itself (via `source`) when the run's own
    // chat/project is already on screen, and is never retained.
    const t = toastForEvent(ev({ kind: "done", target: "/Users/x/proj" }));
    expect(t.tone).toBe("info");
    expect(t.source).toBe("/Users/x/proj");
    expect(t.msg).toContain("finished");
  });

  it("reports a scheduled run starting the same suppressible way", () => {
    const t = toastForEvent(ev({ kind: "started", target: "/Users/x/proj" }));
    expect(t.tone).toBe("info");
    expect(t.source).toBe("/Users/x/proj");
    expect(t.msg).toContain("started");
  });

  it("treats a failure as needing a person, never suppressed", () => {
    const t = toastForEvent(ev({ kind: "failed" }));
    expect(t.tone).toBe("error");
    expect(t.source).toBeUndefined();
    expect(t.msg).toContain("failed");
  });

  it("distinguishes missed from failed in the wording", () => {
    // Nothing went wrong — the app just wasn't running inside the catch-up
    // window — so calling it "failed" would misdescribe it. It still needs a
    // person: the user asked for something that did not happen.
    const t = toastForEvent(ev({ kind: "missed" }));
    expect(t.tone).toBe("error");
    expect(t.msg).toContain("was missed");
    expect(t.msg).not.toContain("failed");
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
