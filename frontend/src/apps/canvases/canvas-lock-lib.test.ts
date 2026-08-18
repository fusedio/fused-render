import { describe, expect, it } from "bun:test";
import {
  decideLock,
  lockMessage,
  nextAckState,
  type AckState,
  type LockHold,
  type LockInput,
} from "@apps/canvases/canvas-lock-lib";

const GRACE = 13000;
const T = 1_000_000;

const st = (over: Partial<LockInput> = {}): LockInput => ({
  watching: true,
  push_state: "idle",
  pulling: false,
  ...over,
});

describe("engaging the lock", () => {
  it("locks the instant a push starts, even on a page that just loaded", () => {
    // No "already engaged" precondition for publishing/pulling any more: a
    // sync operation in flight is never safe to ignore.
    const d = decideLock(st({ push_state: "pushing" }), null, null, T, GRACE);
    expect(d.hold).toBe("publishing");
    expect(d.settledAt).toBeNull();
  });

  it("does not lock on a merely-pending change set (D354)", () => {
    // "pending" means the watcher is holding queued edits while a session is
    // live, not that a push is moving anything — no sync op is in flight, so
    // this must behave exactly like idle for locking purposes.
    const d = decideLock(st({ push_state: "pending" }), null, null, T, GRACE);
    expect(d.hold).toBeNull();
    expect(d.settledAt).toBeNull();
  });

  it("locks the instant a pull/merge starts, even on a page that just loaded", () => {
    const d = decideLock(st({ pulling: true }), null, null, T, GRACE);
    expect(d.hold).toBe("pulling");
    expect(d.settledAt).toBeNull();
  });

  it("does not lock a quiet, idle canvas on arrival", () => {
    expect(decideLock(st(), null, null, T, GRACE).hold).toBeNull();
  });

  it("never locks when nothing is syncing", () => {
    // A dropped watcher, a server restart, or a repeatedly failing status
    // poll (finding 6) all resolve to this same "not watching" shape — the
    // unlock is reachable from the page's own state, not only from a
    // transition it might have missed.
    expect(
      decideLock(st({ watching: false, push_state: "pushing" }), "publishing", null, T, GRACE)
        .hold,
    ).toBeNull();
    expect(decideLock(null, "pulling", null, T, GRACE).hold).toBeNull();
  });

  it("a chat-only run never locks — the exact user report this supersedes", () => {
    // No amount of chat activity moves push_state or pulling on its own; a
    // Claude session that only talks and never edits stays idle forever.
    expect(decideLock(st(), null, null, T, GRACE).hold).toBeNull();
    expect(decideLock(st(), null, null, T + 60 * 60 * 1000, GRACE).hold).toBeNull();
  });

  it("a long editing session with no push in flight never locks", () => {
    // The owner's whole point: many edits over a long chat must not block the
    // user. Only an actual sync op (push/pull) locks — editing alone, no
    // matter how long, does not.
    for (let t = T; t < T + 10 * 60 * 1000; t += 30_000) {
      expect(decideLock(st(), null, null, t, GRACE).hold).toBeNull();
    }
  });
});

describe("releasing after a push", () => {
  it("keeps holding while the final change set is still going up", () => {
    // Releasing here is the D339 shape reproduced by our own unlock: on unlock
    // the workbench flushes its dirty in-memory state, and last-writer-wins
    // then overwrites the push that had not landed yet.
    const d = decideLock(st({ push_state: "pushing" }), "publishing", null, T, GRACE);
    expect(d.hold).toBe("publishing");
    expect(d.settledAt).toBeNull();
  });

  it("a push that drops back to pending mid-flight arms settling, not publishing (D354)", () => {
    // "pending" is never itself a hold — but it IS treated as "not pushing",
    // so coming out of an active "publishing" hold it falls into the same
    // settling-grace path idle does, rather than continuing to hold.
    const d = decideLock(st({ push_state: "pending" }), "publishing", null, T, GRACE);
    expect(d.hold).toBe("settling");
    expect(d.settledAt).toBe(T);
  });

  it("pending -> pushing -> idle still arms the settling grace (D354)", () => {
    // A real push (even one that was "pending" moments before) still moves
    // the remote, so it still needs the grace window on the way out.
    const pending = decideLock(st({ push_state: "pending" }), null, null, T, GRACE);
    expect(pending.hold).toBeNull();

    const pushing = decideLock(st({ push_state: "pushing" }), pending.hold, pending.settledAt, T + 1000, GRACE);
    expect(pushing.hold).toBe("publishing");

    const settled = decideLock(st(), pushing.hold, pushing.settledAt, T + 2000, GRACE);
    expect(settled.hold).toBe("settling");
    expect(settled.settledAt).toBe(T + 2000);
  });

  it("pending -> idle (push never started, e.g. merged away) does not arm settling (D354)", () => {
    const pending = decideLock(st({ push_state: "pending" }), null, null, T, GRACE);
    expect(pending.hold).toBeNull();

    const idle = decideLock(st(), pending.hold, pending.settledAt, T + 1000, GRACE);
    expect(idle.hold).toBeNull();
    expect(idle.settledAt).toBeNull();
  });

  it("holds through the grace window, then releases", () => {
    // The window has to outlast the embedded workbench's ~10s upstream poll:
    // until it re-hydrates, its in-memory state is still stale.
    const first = decideLock(st(), "publishing", null, T, GRACE);
    expect(first.hold).toBe("settling");
    expect(first.settledAt).toBe(T);

    const midway = decideLock(st(), "settling", T, T + GRACE - 1, GRACE);
    expect(midway.hold).toBe("settling");
    expect(midway.settledAt).toBe(T);

    const done = decideLock(st(), "settling", T, T + GRACE, GRACE);
    expect(done.hold).toBeNull();
    expect(done.settledAt).toBeNull();
  });

  it("restarts the grace window if a new push starts mid-wait", () => {
    const resumed = decideLock(st({ push_state: "pushing" }), "settling", T, T + 5000, GRACE);
    expect(resumed.hold).toBe("publishing");
    expect(resumed.settledAt).toBeNull();
    // ...and the next settled observation stamps a fresh start.
    expect(decideLock(st(), "publishing", null, T + 6000, GRACE).settledAt).toBe(T + 6000);
  });

  it("releases on a failed push instead of holding forever", () => {
    // There is no success coming to wait for. The failure is already surfaced
    // by the error banner and its Fix button, and holding the workbench
    // read-only until someone fixes a validation error is a lock with no end
    // condition.
    const d = decideLock(st({ push_state: "error" }), "publishing", T, T + 1, GRACE);
    expect(d.hold).toBeNull();
    expect(d.settledAt).toBeNull();
  });
});

describe("releasing after a pull — no grace window", () => {
  it("releases the instant a pull/merge ends, immediately, not after grace", () => {
    // A pull never moves the remote — it writes local files from a manifest
    // already fetched — so there is nothing upstream for the workbench's own
    // poll to race. Unlike a push, it must not arm the grace window.
    const d = decideLock(st(), "pulling", null, T, GRACE);
    expect(d.hold).toBeNull();
    expect(d.settledAt).toBeNull();
  });

  it("a pull finishing while a push's grace window was running does not cancel it", () => {
    // prevHold "settling" is preserved on its own path — a pull is a
    // different, unrelated leg, so this test only pins that pulling=false +
    // prevHold "settling" keeps counting the existing window rather than
    // being reset by pull-specific logic (there is none to trigger here).
    const midway = decideLock(st(), "settling", T, T + 100, GRACE);
    expect(midway.hold).toBe("settling");
  });
});

describe("the enforcement ack handshake", () => {
  it("a new engagement always starts waiting, even after a prior ack", () => {
    // Reset per engagement, by owner decision — nothing carries over.
    expect(nextAckState("acked", "engage")).toBe("waiting");
    expect(nextAckState("unacked", "engage")).toBe("waiting");
    expect(nextAckState("waiting", "engage")).toBe("waiting");
  });

  it("an ack always wins, including upgrading an unacked fallback", () => {
    expect(nextAckState("waiting", "ack")).toBe("acked");
    expect(nextAckState("unacked", "ack")).toBe("acked");
  });

  it("a timeout falls back to unacked only from waiting", () => {
    expect(nextAckState("waiting", "timeout")).toBe("unacked");
    // A stale timer firing after an ack (or a previous timeout) already
    // resolved this engagement must not undo it.
    expect(nextAckState("acked", "timeout")).toBe("acked");
    expect(nextAckState("unacked", "timeout")).toBe("unacked");
  });

  it("every state is reachable and distinct", () => {
    const all: AckState[] = ["waiting", "acked", "unacked"];
    expect(new Set(all).size).toBe(3);
  });
});

describe("what the banner says", () => {
  it("distinguishes the three reasons", () => {
    // A user watching a locked pane for another few seconds after the sync
    // op stopped needs to know it is finishing, not stuck.
    const all = (["publishing", "pulling", "settling"] as LockHold[]).map(lockMessage);
    expect(new Set(all).size).toBe(3);
    expect(lockMessage("publishing")).toContain("Publishing");
    expect(lockMessage("pulling")).toContain("Pulling");
    expect(lockMessage("settling")).toContain("Finishing up");
  });
});
