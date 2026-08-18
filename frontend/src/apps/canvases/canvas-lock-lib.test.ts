import { describe, expect, it } from "bun:test";
import {
  decideLock,
  lockMessage,
  type LockInput,
} from "@apps/canvases/canvas-lock-lib";

const GRACE = 13000;
const T = 1_000_000;

const st = (over: Partial<LockInput> = {}): LockInput => ({
  watching: true,
  agent_active: false,
  push_state: "idle",
  ...over,
});

describe("engaging the lock", () => {
  it("locks while a session is live", () => {
    const d = decideLock(st({ agent_active: true }), false, null, T, GRACE);
    expect(d.hold).toBe("editing");
  });

  it("does not lock a quiet canvas on arrival", () => {
    // The bug this guards: `publishing`/`settling` only EXTEND a lock. If they
    // could start one, opening a canvas that had merely pushed recently would
    // black out the workbench for a whole grace window for no reason.
    expect(decideLock(st(), false, null, T, GRACE).hold).toBeNull();
    expect(decideLock(st({ push_state: "pending" }), false, null, T, GRACE).hold)
      .toBeNull();
  });

  it("never locks when nothing is syncing", () => {
    // A dropped watcher or a server restart must not leave the pane read-only
    // with nothing left to release it — so the unlock is reachable from the
    // page's own state, not only from a transition it might have missed.
    expect(decideLock(st({ watching: false, agent_active: true }), true, null, T, GRACE).hold)
      .toBeNull();
    expect(decideLock(null, true, null, T, GRACE).hold).toBeNull();
  });
});

describe("releasing the lock", () => {
  it("keeps holding while the final change set is still going up", () => {
    // Releasing here is the D339 shape reproduced by our own unlock: on unlock
    // the workbench flushes its dirty in-memory state, and last-writer-wins
    // then overwrites the push that had not landed yet.
    for (const push_state of ["pending", "pushing"] as const) {
      const d = decideLock(st({ push_state }), true, null, T, GRACE);
      expect(d.hold).toBe("publishing");
      expect(d.settledAt).toBeNull();
    }
  });

  it("holds through the grace window, then releases", () => {
    // The window has to outlast the embedded workbench's ~10s upstream poll:
    // until it re-hydrates, its in-memory state is still stale.
    const first = decideLock(st(), true, null, T, GRACE);
    expect(first.hold).toBe("settling");
    expect(first.settledAt).toBe(T);

    const midway = decideLock(st(), true, T, T + GRACE - 1, GRACE);
    expect(midway.hold).toBe("settling");
    expect(midway.settledAt).toBe(T);

    const done = decideLock(st(), true, T, T + GRACE, GRACE);
    expect(done.hold).toBeNull();
    expect(done.settledAt).toBeNull();
  });

  it("restarts the grace window if work resumes mid-wait", () => {
    // A second turn, or a push arriving after the first looked settled: the
    // window must start again, not run out on the old stamp.
    expect(decideLock(st({ agent_active: true }), true, T, T + 5000, GRACE).settledAt)
      .toBeNull();
    expect(decideLock(st({ push_state: "pushing" }), true, T, T + 5000, GRACE).settledAt)
      .toBeNull();
    // ...and the next settled observation stamps a fresh start.
    expect(decideLock(st(), true, null, T + 6000, GRACE).settledAt).toBe(T + 6000);
  });

  it("releases on a failed push instead of holding forever", () => {
    // There is no success coming to wait for. The failure is already surfaced
    // by the error banner and its Fix button, and holding the workbench
    // read-only until someone fixes a validation error is a lock with no end
    // condition.
    const d = decideLock(st({ push_state: "error" }), true, T, T + 1, GRACE);
    expect(d.hold).toBeNull();
    expect(d.settledAt).toBeNull();
  });

  it("a live session outranks a failed push", () => {
    const d = decideLock(
      st({ agent_active: true, push_state: "error" }), true, null, T, GRACE);
    expect(d.hold).toBe("editing");
  });
});

describe("what the banner says", () => {
  it("distinguishes the three reasons", () => {
    // A user watching a locked pane for another few seconds after the chat
    // stopped needs to know it is finishing, not stuck.
    const all = (["editing", "publishing", "settling"] as const).map(lockMessage);
    expect(new Set(all).size).toBe(3);
    expect(lockMessage("editing")).toContain("editing");
    expect(lockMessage("publishing")).toContain("Publishing");
    expect(lockMessage("settling")).toContain("Finishing up");
  });
});
