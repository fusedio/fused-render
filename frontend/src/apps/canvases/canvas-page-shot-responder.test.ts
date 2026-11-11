// The canvas page answers `canvas_screenshot` for every chat on its canvas
// (D1471): claim priority, the one-at-a-time queue, the honest refusals, and
// the capture itself. Every capture below ends in an error answer, which skips
// the shots upload (attach.ts's, not exercised here).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

import type { CanvasShot } from "@apps/claude";
import type { CanvasShotInboxRow } from "@apps/claude/protocol/types";

const {
  CLAIM_DELAY_MS,
  HIDDEN_SENTENCE,
  SHOT_WAIT_MS,
  claimDelayMs,
  claimDue,
  createPageShotResponder,
  requestExpired,
  takeCanvasShot,
  workbenchErrorSentence,
} = await import("./canvas-page-shot-responder");
const { canvasPagePath } = await import("./api");
const { openCanvasPath } = await import("@apps/claude/ui/OpenCanvasCard");

const NOW = 1_000_000_000_000;
const row = (id: string, ageMs = 10_000, claimed = false): CanvasShotInboxRow => ({
  id,
  runId: "run-" + id,
  what: "canvas",
  udf: "",
  reason: "",
  created_at: (NOW - ageMs) / 1000,
  claimed,
});

test("a focused page claims at once, a visible one later, a hidden one last", () => {
  const focused = { visible: true, focused: true };
  const visible = { visible: true, focused: false };
  const hidden = { visible: false, focused: false };
  expect(claimDelayMs(focused)).toBe(0);
  expect(claimDelayMs(visible)).toBe(CLAIM_DELAY_MS.visible);
  expect(claimDelayMs(hidden)).toBe(CLAIM_DELAY_MS.hidden);
  expect(claimDelayMs(focused) < claimDelayMs(visible) && claimDelayMs(visible) < claimDelayMs(hidden)).toBe(true);
  // 300 ms after the request: only the focused page may take it.
  const r = row("r", 300);
  expect(claimDue(r, focused, NOW)).toBe(true);
  expect(claimDue(r, visible, NOW)).toBe(false);
  expect(claimDue(r, hidden, NOW)).toBe(false);
  // 2.5 s on, the hidden page may too (the front one never claimed).
  expect(claimDue(row("r", 2500), hidden, NOW)).toBe(true);
});

type Deps = Parameters<typeof createPageShotResponder>[0];

function harness(over: Partial<Deps> & { rows: CanvasShotInboxRow[] }) {
  const log: string[] = [];
  const answers: Array<[string, unknown]> = [];
  const page = { visible: true, focused: true, frameUp: true, error: "" };
  const r = createPageShotResponder({
    page: () => page,
    now: () => NOW,
    inbox: async () => over.rows,
    claim: async (x) => {
      log.push("claim " + x.id);
      return true;
    },
    take: async (x) => {
      log.push("take " + x.id);
      await new Promise((res) => setTimeout(res, 5));
      log.push("took " + x.id);
      return { error: "drawn " + x.id };
    },
    answer: async (x, a) => {
      answers.push([x.id, a]);
      return true;
    },
    ...over,
  });
  return { r, log, answers, page, rows: over.rows };
}

test("claimed requests are captured one at a time, in order — never 'already being taken'", async () => {
  const h = harness({ rows: [row("a"), row("b")] });
  await h.r.tick();
  await h.r.idle();
  expect(h.log).toEqual(["claim a", "claim b", "take a", "took a", "take b", "took b"]);
  expect(h.answers.map(([id]) => id)).toEqual(["a", "b"]);
  // Answered: a row still listed (the server drops it a beat later) is not shot again.
  h.rows.splice(0, h.rows.length, row("a", 10_000, true));
  await h.r.tick();
  await h.r.idle();
  expect(h.answers.length).toBe(2);
});

test("a request another page won is left alone; one not yet due is not claimed", async () => {
  const h = harness({ rows: [row("won", 10_000), row("young", 100)], claim: async () => false });
  h.page.focused = false; // visible: 500 ms
  await h.r.tick();
  await h.r.idle();
  expect(h.answers).toEqual([]);
});

test("a request past the tool's own wait is never claimed or captured, even one queued behind a slow capture", async () => {
  // Already expired when the page polls: no claim, no capture, no answer.
  const old = harness({ rows: [row("old", SHOT_WAIT_MS + 1000)] });
  await old.r.tick();
  await old.r.idle();
  expect(old.log).toEqual([]);
  expect(old.answers).toEqual([]);
  // Fresh when queued, expired by the time the first capture frees the chain.
  let clock = NOW;
  const h = harness({
    rows: [row("slow", 1000), row("late", SHOT_WAIT_MS - 2000)],
    now: () => clock,
    take: async (x) => {
      clock += 5000;
      return { error: "drawn " + x.id };
    },
  });
  await h.r.tick();
  await h.r.idle();
  expect(h.answers.map(([id]) => id)).toEqual(["slow"]);
});

test("an answer that did not land is re-sent from the picture already taken", async () => {
  let landed = false;
  let takes = 0;
  const h = harness({
    rows: [row("a")],
    take: async () => {
      takes += 1;
      return { error: "x" };
    },
    answer: async () => {
      const ok = landed;
      landed = true;
      return ok;
    },
  });
  await h.r.tick();
  await h.r.idle();
  await h.r.tick(); // the row comes back, still this page's
  await h.r.idle();
  expect(takes).toBe(1);
  expect(landed).toBe(true);
});

test("a page that cannot load the workbench says so at once, and a hidden one names the window", async () => {
  const h = harness({ rows: [row("a")] });
  h.page.frameUp = false;
  h.page.error = "not signed in";
  await h.r.tick();
  await h.r.idle();
  expect(h.answers[0]).toEqual(["a", { error: workbenchErrorSentence("not signed in") }]);

  const hidden = harness({ rows: [row("b")] });
  hidden.page.visible = false;
  await hidden.r.tick();
  await hidden.r.idle();
  expect(hidden.answers[0]).toEqual(["b", { error: HIDDEN_SENTENCE }]);

  // Hidden, but the workbench never came up: that is the sentence, not the window.
  const cold = harness({ rows: [row("c")] });
  cold.page.visible = false;
  cold.page.frameUp = false;
  await cold.r.tick();
  await cold.r.idle();
  expect(cold.answers[0]).toEqual(["c", { error: "drawn c" }]);
});

test("takeCanvasShot: a refused capture is the answer's error, tried once more when the workbench drew nothing", async () => {
  let n = 0;
  const shot: CanvasShot = {
    targets: ["canvas"],
    capture: async () => {
      n += 1;
      return n === 1 ? { ok: false, error: "the canvas could not be captured" } : { ok: false, error: "still no" };
    },
  };
  expect(await takeCanvasShot(shot, { what: "canvas", udf: "" })).toEqual({ error: "still no" });
  expect(n).toBe(2);
});

test("takeCanvasShot: a UDF with no name from the canvas chat is refused with the fix", async () => {
  const shot: CanvasShot = { targets: ["canvas"], capture: async () => ({ ok: false, error: "never" }) };
  const out = await takeCanvasShot(shot, { what: "udf", udf: "" });
  expect("error" in out && out.error).toContain("name the UDF");
});

test("the open-canvas card goes where canvasPagePath does", () => {
  expect(openCanvasPath("prod", "demo")).toBe(canvasPagePath("prod", "demo"));
  expect(openCanvasPath("unstable", "a_b")).toBe(canvasPagePath("unstable", "a_b"));
});

test("takeCanvasShot: the picture's framing rides the answer (the tool's note says what it is)", async () => {
  const real = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    const body = String(url).includes("/api/claude/agent") ? { dir: "/tmp/shots" } : { ok: true };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    const shot: CanvasShot = { targets: ["canvas"], capture: async () => ({ ok: true, blob, framing: "whole-frame" }) };
    const out = await takeCanvasShot(shot, { what: "udf", udf: "udf_2" });
    expect("image" in out && out.image.startsWith("/tmp/shots/")).toBe(true);
    expect(out).toMatchObject({ mime: "image/png", udf: "udf_2", framing: "whole-frame" });
  } finally {
    globalThis.fetch = real;
  }
});

test("the server's deadline wins over created_at: a late Open is still served", () => {
  const late = { ...row("r", SHOT_WAIT_MS + 5000), deadline: (NOW + 60_000) / 1000 };
  expect(requestExpired(late, NOW)).toBe(false);
  expect(requestExpired({ ...late, deadline: (NOW - 1) / 1000 }, NOW)).toBe(true);
  expect(requestExpired(row("r", SHOT_WAIT_MS + 5000), NOW)).toBe(true);
});

test("a page that lost focus during the poll claims by its standing now", async () => {
  const h = harness({ rows: [row("r", 300)] });
  h.r = createPageShotResponder({
    page: () => h.page,
    now: () => NOW,
    inbox: async () => {
      h.page.focused = false; // focus moved while the poll was out
      return [row("r", 300)];
    },
    claim: async (x) => {
      h.log.push("claim " + x.id);
      return true;
    },
    take: async () => ({ error: "x" }),
    answer: async () => true,
  });
  await h.r.tick();
  expect(h.log).toEqual([]);
});

test("a stopped page claims and captures nothing more", async () => {
  let release!: () => void;
  const gate = new Promise<void>((res) => (release = res));
  const h = harness({ rows: [row("a")] });
  h.r = createPageShotResponder({
    page: () => h.page,
    now: () => NOW,
    inbox: async () => {
      await gate;
      return [row("a")];
    },
    claim: async (x) => {
      h.log.push("claim " + x.id);
      return true;
    },
    take: async (x) => {
      h.log.push("take " + x.id);
      return { error: "x" };
    },
    answer: async () => true,
  });
  const t = h.r.tick();
  h.r.stop(); // the page left while its poll was out
  release();
  await t;
  await h.r.idle();
  expect(h.log).toEqual([]);
});
