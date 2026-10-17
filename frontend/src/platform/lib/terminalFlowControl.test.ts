import { describe, expect, test } from "bun:test";

import {
  FLOW_HIGH_BYTES,
  FLOW_LOW_BYTES,
  REPAUSE_MS,
  createFlowControl,
} from "@platform/lib/terminalFlowControl";

function setup() {
  const calls: string[] = [];
  const fc = createFlowControl({
    pause: () => calls.push("pause"),
    resume: () => calls.push("resume"),
  });
  return { calls, fc };
}

describe("terminal flow control", () => {
  test("stays quiet at or below the high watermark", () => {
    const { calls, fc } = setup();
    fc.chunk(FLOW_HIGH_BYTES);
    expect(calls).toEqual([]);
  });

  test("pauses once when pending crosses HIGH, however many chunks follow", () => {
    const { calls, fc } = setup();
    fc.chunk(FLOW_HIGH_BYTES);
    fc.chunk(1);
    fc.chunk(500_000);
    expect(calls).toEqual(["pause"]);
  });

  test("resumes once when completions drain pending below LOW", () => {
    const { calls, fc } = setup();
    const done = [fc.chunk(600_000), fc.chunk(600_000)]; // 1.2 MB: paused
    expect(calls).toEqual(["pause"]);
    done[0]!(); // 600 kB left: still above LOW
    expect(calls).toEqual(["pause"]);
    done[1]!(); // 0 left
    expect(calls).toEqual(["pause", "resume"]);
    fc.chunk(10)();
    expect(calls).toEqual(["pause", "resume"]);
  });

  test("does not resume while pending sits between LOW and HIGH", () => {
    const { calls, fc } = setup();
    const a = fc.chunk(FLOW_HIGH_BYTES + 1);
    const b = fc.chunk(FLOW_LOW_BYTES + 1000);
    a();
    expect(calls).toEqual(["pause"]);
    b();
    expect(calls).toEqual(["pause", "resume"]);
  });

  test("reset forgets in-flight bytes and ignores their late callbacks", () => {
    const { calls, fc } = setup();
    const stale = fc.chunk(FLOW_HIGH_BYTES + 1);
    expect(calls).toEqual(["pause"]);
    fc.reset();
    stale(); // must not resume or drive pending negative
    expect(calls).toEqual(["pause"]);
    // Fresh accounting: a new flood pauses again.
    fc.chunk(FLOW_HIGH_BYTES + 1);
    expect(calls).toEqual(["pause", "pause"]);
  });

  describe("re-pause after the server's pause expiry", () => {
    function clocked() {
      const calls: string[] = [];
      let t = 1000;
      const fc = createFlowControl(
        { pause: () => calls.push("pause"), resume: () => calls.push("resume") },
        { now: () => t },
      );
      return { calls, fc, advance: (ms: number) => (t += ms) };
    }

    test("still above HIGH past REPAUSE_MS sends a second pause", () => {
      const { calls, fc, advance } = clocked();
      fc.chunk(FLOW_HIGH_BYTES + 1);
      advance(REPAUSE_MS);
      fc.chunk(10);
      expect(calls).toEqual(["pause", "pause"]);
      advance(REPAUSE_MS - 1);
      fc.chunk(10);
      expect(calls).toEqual(["pause", "pause"]);
    });

    test("under REPAUSE_MS sends none", () => {
      const { calls, fc, advance } = clocked();
      fc.chunk(FLOW_HIGH_BYTES + 1);
      advance(REPAUSE_MS - 1);
      fc.chunk(10);
      expect(calls).toEqual(["pause"]);
    });

    test("REPAUSE_MS stays below the server's 5 s expiry", () => {
      expect(REPAUSE_MS).toBeLessThan(5000);
    });

    test("draining below LOW resumes once and never re-pauses", () => {
      const { calls, fc, advance } = clocked();
      const a = fc.chunk(FLOW_HIGH_BYTES + 1);
      advance(REPAUSE_MS * 3);
      a();
      expect(calls).toEqual(["pause", "resume"]);
      advance(REPAUSE_MS * 3);
      fc.chunk(10);
      expect(calls).toEqual(["pause", "resume"]);
    });
  });
});
