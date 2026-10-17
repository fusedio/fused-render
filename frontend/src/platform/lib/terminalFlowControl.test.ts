import { describe, expect, test } from "bun:test";

import {
  FLOW_HIGH_BYTES,
  FLOW_LOW_BYTES,
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
});
