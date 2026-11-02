// The canvas-screenshot pull channel's two postures: a canvas page answers at
// once through its capture; a page with no canvas waits its polls out and
// then answers the "open the canvas" sentence. The upload itself is the
// ordinary shots upload (attach.ts), not exercised here — every path below
// ends in an error answer, which skips it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import type { CanvasShotRow } from "../protocol/types";
import type { CanvasShot } from "../shots/canvas-shot";

const { useCanvasShotResponder, CANVAS_SHOT_WAIT_S, NO_CANVAS_SENTENCE } =
  await import("./useCanvasShotResponder");

const row = (id: string, pollsSeen: number, what: "canvas" | "udf" = "canvas", udf = "", ageS = 0): CanvasShotRow => ({
  id, what, udf, reason: "", created_at: Date.now() / 1000 - ageS, pollsSeen,
});

type Answer = { image: string; mime: string; udf?: string } | { error: string };

function mount(initial: { rows: CanvasShotRow[]; shot: CanvasShot | null }, answers: Array<[string, Answer]>) {
  function Probe(props: { rows: CanvasShotRow[]; shot: CanvasShot | null }) {
    useCanvasShotResponder({
      rows: props.rows,
      shot: props.shot,
      answer: async (id, a) => {
        answers.push([id, a]);
        return true;
      },
    });
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe {...initial} />);
  });
  return {
    async poll(next: { rows: CanvasShotRow[]; shot: CanvasShot | null }) {
      await act(async () => {
        r.update(<Probe {...next} />);
        await Promise.resolve();
      });
    },
  };
}

const settle = () => new Promise((res) => setTimeout(res, 20));

test("a page with no canvas waits the capture budget out, then says to open the canvas", async () => {
  const answers: Array<[string, Answer]> = [];
  const h = mount({ rows: [row("r1", 1)], shot: null }, answers);
  await settle();
  expect(answers).toEqual([]);
  await h.poll({ rows: [row("r1", 2, "canvas", "", CANVAS_SHOT_WAIT_S - 5)], shot: null });
  await settle();
  expect(answers).toEqual([]); // still the canvas page's turn
  await h.poll({ rows: [row("r1", 3, "canvas", "", CANVAS_SHOT_WAIT_S + 1)], shot: null });
  await settle();
  expect(answers).toEqual([["r1", { error: NO_CANVAS_SENTENCE }]]);
  // Claimed once: another poll of the same row answers nothing twice.
  await h.poll({ rows: [row("r1", 4, "canvas", "", CANVAS_SHOT_WAIT_S + 2)], shot: null });
  await settle();
  expect(answers.length).toBe(1);
});

test("an answer that did not land is retried from the picture already taken", async () => {
  let captures = 0;
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: async () => {
      captures += 1;
      return { ok: false, error: "once" };
    },
  };
  const answers: Array<[string, Answer]> = [];
  let landed = false;
  function Probe(props: { rows: CanvasShotRow[] }) {
    useCanvasShotResponder({
      rows: props.rows,
      shot,
      answer: async (id, a) => {
        answers.push([id, a]);
        return landed;
      },
    });
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe rows={[row("r1", 1)]} />);
  });
  await settle();
  expect(answers.length).toBe(1); // first try, refused by the server
  landed = true;
  await act(async () => {
    r.update(<Probe rows={[row("r1", 2)]} />); // the row re-surfaces
    await Promise.resolve();
  });
  await settle();
  expect(answers.length).toBe(2);
  expect(captures).toBe(1); // re-answered, not re-captured
});

test("a canvas page answers at once, and a refused capture is the answer's error", async () => {
  const asked: Array<[string, string | undefined, string | undefined]> = [];
  const shot: CanvasShot = {
    targets: ["canvas", "udf"],
    capture: async (what, udf, by) => {
      asked.push([what, udf, by]);
      return { ok: false, error: "the workbench is not loaded yet" };
    },
  };
  const answers: Array<[string, Answer]> = [];
  mount({ rows: [row("r1", 1, "udf", "fine_udf")], shot }, answers);
  await settle();
  // AS THE AGENT: the host's scrim over the workbench names who is looking,
  // because no gesture of the user's is behind this capture.
  expect(asked).toEqual([["udf", "fine_udf", "agent"]]);
  expect(answers).toEqual([["r1", { error: "the workbench is not loaded yet" }]]);
});

test("a UDF picture with no name on the canvas-only chat is refused with the fix", async () => {
  let captures = 0;
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: async () => {
      captures += 1;
      return { ok: false, error: "unreachable" };
    },
  };
  const answers: Array<[string, Answer]> = [];
  mount({ rows: [row("r1", 1, "udf", "")], shot }, answers);
  await settle();
  expect(captures).toBe(0);
  expect(answers.length).toBe(1);
  expect("error" in answers[0]![1] && answers[0]![1].error).toContain("name the UDF");
});

test("a canvas page that comes up after the request answers it on the next poll", async () => {
  const answers: Array<[string, Answer]> = [];
  const h = mount({ rows: [row("r1", 1)], shot: null }, answers);
  await settle();
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: async () => ({ ok: false, error: "no frame" }),
  };
  await h.poll({ rows: [row("r1", 2)], shot });
  await settle();
  expect(answers).toEqual([["r1", { error: "no frame" }]]);
});

test("a capture that outlives a poll still answers (the effect re-runs every tick)", async () => {
  // The bug this pins: the row's poll count advances every 400 ms, the effect
  // re-runs, and a cleanup-cancelled answer left the tool blocked for 75 s
  // with the picture in hand. The capture must run to its answer regardless.
  let release!: () => void;
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: () =>
      new Promise((res) => {
        release = () => res({ ok: false, error: "slow but done" });
      }),
  };
  const answers: Array<[string, Answer]> = [];
  const h = mount({ rows: [row("r1", 1)], shot }, answers);
  await settle();
  await h.poll({ rows: [row("r1", 2)], shot }); // a tick while the capture is out
  await h.poll({ rows: [row("r1", 3)], shot });
  await settle();
  expect(answers).toEqual([]);
  release();
  await settle();
  expect(answers).toEqual([["r1", { error: "slow but done" }]]);
});

test("a capture the workbench refused once is tried once more", async () => {
  let n = 0;
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: async () => {
      n += 1;
      return n === 1
        ? { ok: false, error: "the canvas could not be captured" }
        : { ok: false, error: "still no" };
    },
  };
  const answers: Array<[string, Answer]> = [];
  mount({ rows: [row("r1", 1)], shot }, answers);
  await new Promise((res) => setTimeout(res, 1800));
  expect(n).toBe(2);
  expect(answers).toEqual([["r1", { error: "still no" }]]);
});

test("the answer carries the run that asked", async () => {
  const shot: CanvasShot = { targets: ["canvas"], capture: async () => ({ ok: false, error: "nope" }) };
  const seen: Array<string | undefined> = [];
  function Probe() {
    useCanvasShotResponder({
      rows: [{ ...row("r1", 1), runId: "run-A" }],
      shot,
      answer: async (_id, _a, runId) => {
        seen.push(runId);
        return true;
      },
    });
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  await settle();
  expect(seen).toEqual(["run-A"]);
});
