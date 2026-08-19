import { describe, expect, it } from "bun:test";
import {
  decideLock,
  lockMessage,
  nextAckState,
  reengagedWithinGrace,
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

describe("holds coalesce across consecutive operations", () => {
  it("a pull ending arms the grace window too, not just a push", () => {
    // Superseding the push-only rule: a pull releasing outright is what let
    // the pane unlock between the two legs of one publish. The extra hold
    // after a LONE pull is the accepted cost of not flickering.
    const d = decideLock(st(), "pulling", null, T, GRACE);
    expect(d.hold).toBe("settling");
    expect(d.settledAt).toBe(T);
  });

  it("pull -> push is ONE engagement, never released in between", () => {
    // The real shape of a publish: pull, resolve, validate, push. Every step
    // must return a non-null hold, or the workbench visibly unlocks mid-way.
    let hold: LockHold | null = null;
    let settledAt: number | null = null;
    const step = (status: LockInput, now: number) => {
      const d = decideLock(status, hold, settledAt, now, GRACE);
      hold = d.hold;
      settledAt = d.settledAt;
      return d.hold;
    };
    expect(step(st({ pulling: true }), T)).toBe("pulling");
    // The pull ends; the push has not started yet (a real gap of a second or
    // two while the merge validates).
    expect(step(st(), T + 500)).toBe("settling");
    expect(step(st({ push_state: "pending" }), T + 1500)).toBe("settling");
    expect(step(st({ push_state: "pushing" }), T + 2500)).toBe("publishing");
    expect(step(st(), T + 4000)).toBe("settling");
    // ...and it does eventually release, once nothing has happened for a
    // whole window.
    expect(step(st(), T + 4000 + GRACE)).toBeNull();
  });

  it("N pushes inside one window are one hold, not N lock/unlock cycles", () => {
    let hold: LockHold | null = null;
    let settledAt: number | null = null;
    let releases = 0;
    for (let i = 0; i < 4; i += 1) {
      // pushing → idle → pushing → idle …, each pair well inside GRACE.
      for (const status of [st({ push_state: "pushing" }), st()]) {
        const d = decideLock(status, hold, settledAt, T + i * 1000, GRACE);
        if (d.hold === null) releases += 1;
        hold = d.hold;
        settledAt = d.settledAt;
      }
    }
    expect(releases).toBe(0);
    // Only the window running out releases it.
    expect(decideLock(st(), hold, settledAt, T + 4000 + GRACE, GRACE).hold).toBeNull();
  });

  it("still never locks a canvas that was not holding anything", () => {
    // The coalescing rule must not become "lock on arithmetic": with no
    // previous hold there is nothing to keep holding.
    expect(decideLock(st(), null, null, T, GRACE).hold).toBeNull();
    expect(decideLock(st(), null, T - 1, T, GRACE).hold).toBeNull();
  });

  it("a dropped watcher or a failed push still releases immediately", () => {
    // Coalescing must not outrank the two unconditional releases: those are
    // the only ways out of a lock the user cannot otherwise clear.
    expect(decideLock(st({ watching: false }), "pulling", T, T + 1, GRACE).hold).toBeNull();
    expect(decideLock(st({ push_state: "error" }), "pulling", T, T + 1, GRACE).hold).toBeNull();
  });
});

describe("the enforcement ack handshake", () => {
  it("a genuinely new engagement starts waiting, even after a prior ack", () => {
    // Nothing carries over across a full release: the capability is a fact
    // about the deployed workbench that this page assumes nothing about.
    expect(nextAckState("acked", "engage")).toBe("waiting");
    expect(nextAckState("unacked", "engage")).toBe("waiting");
    expect(nextAckState("waiting", "engage")).toBe("waiting");
  });

  it("a re-engagement inside the grace window keeps the ack it already had", () => {
    // Otherwise the fallback scrim flashes back on for LOCK_ACK_TIMEOUT_MS in
    // the middle of a publish whose lock never really let go — the same
    // flicker the coalescing rule removes, one layer up.
    expect(nextAckState("acked", "engage", true)).toBe("acked");
    expect(nextAckState("unacked", "engage", true)).toBe("unacked");
    expect(nextAckState("waiting", "engage", true)).toBe("waiting");
  });

  it("an ack or a timeout is unaffected by the grace flag", () => {
    expect(nextAckState("waiting", "ack", true)).toBe("acked");
    expect(nextAckState("waiting", "timeout", true)).toBe("unacked");
  });

  it("what counts as the same engagement is one grace window from the release", () => {
    expect(reengagedWithinGrace(null, T, GRACE)).toBe(false);
    expect(reengagedWithinGrace(T, T, GRACE)).toBe(true);
    expect(reengagedWithinGrace(T, T + GRACE - 1, GRACE)).toBe(true);
    expect(reengagedWithinGrace(T, T + GRACE, GRACE)).toBe(false);
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
