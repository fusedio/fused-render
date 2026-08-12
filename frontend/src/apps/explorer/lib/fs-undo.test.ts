// The undo stack for RELOCATIONS, and the arithmetic of inverting one.
//
// Everything here is about the two properties that make undo trustworthy: the
// inverse is the exact path the entry came from (never a deduped one), and an
// entry that cannot be undone leaves the stack rather than sitting at the top
// failing forever.
import { beforeEach, expect, test } from "bun:test";

// The apply path renames through the api layer, so the fetch stub goes in before
// the (therefore dynamic) import — the same trade fs-move.test.ts makes.
type Req = { url: string; body: { src?: string; dst?: string } };
const posts: Req[] = [];
// Paths whose rename must fail, and with what — how a 404 (source gone) and a
// 409 (something is in the way now) are driven.
let failFor: { src: string; status: number; error: string } | null = null;

(globalThis as { fetch?: unknown }).fetch = ((url: string, init?: { body?: string }) => {
  const body = init?.body ? JSON.parse(init.body) : {};
  posts.push({ url, body });
  if (failFor && body.src === failFor.src) {
    return Promise.resolve({
      ok: false,
      status: failFor.status,
      json: () => Promise.resolve({ error: failFor!.error }),
    });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ path: body.dst }) });
}) as unknown as typeof fetch;

const {
  applyFsOp,
  invertFsOp,
  pushRedoOp,
  pushUndoOp,
  recordFsOp,
  resetFsUndo,
  takeRedoOp,
  takeUndoOp,
  UNDO_CAP,
} = await import("@apps/explorer/lib/fs-undo");

const renames = () =>
  posts.filter((p) => p.url === "/api/fs/rename").map((p) => [p.body.src, p.body.dst]);

beforeEach(() => {
  resetFsUndo();
  posts.length = 0;
  failFor = null;
});

test("inverting an op swaps every pair and reverses the order", () => {
  // Reversed because a batch has to come apart in the opposite order it went
  // together — the same reason an undo of nested operations unwinds inside-out.
  expect(
    invertFsOp({
      kind: "move",
      pairs: [
        { from: "/a/x.md", to: "/b/x.md" },
        { from: "/a/y.md", to: "/b/y.md" },
      ],
    })
  ).toEqual({
    kind: "move",
    pairs: [
      { from: "/b/y.md", to: "/a/y.md" },
      { from: "/b/x.md", to: "/a/x.md" },
    ],
  });
});

test("inverting twice is the original op", () => {
  const op = {
    kind: "rename" as const,
    pairs: [{ from: "/w/notes.md", to: "/w/notes-final.md" }],
  };
  expect(invertFsOp(invertFsOp(op))).toEqual(op);
});

test("a recorded op is what undo takes, and taking it empties the stack", () => {
  const op = { kind: "move" as const, pairs: [{ from: "/a/x.md", to: "/b/x.md" }] };
  recordFsOp(op);
  expect(takeUndoOp()).toEqual(op);
  expect(takeUndoOp()).toBeNull();
});

test("recording an op DROPS the redo stack — that future is gone", () => {
  // The standard rule: doing something new after an undo makes the undone work
  // unreachable, because redoing it would now be redoing it onto a different
  // filesystem than the one it was recorded against.
  recordFsOp({ kind: "move", pairs: [{ from: "/a/x.md", to: "/b/x.md" }] });
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/x.md", to: "/a/x.md" }] });
  recordFsOp({ kind: "rename", pairs: [{ from: "/a/y.md", to: "/a/z.md" }] });
  expect(takeRedoOp()).toBeNull();
});

test("pushing back onto a stack after a redo keeps the OTHER stack", () => {
  // pushUndoOp is not recordFsOp: a redo puts the op back on the undo stack
  // without declaring a new future, so the redo entries below it survive.
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/1", to: "/a/1" }] });
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/2", to: "/a/2" }] });
  const op = takeRedoOp();
  expect(op).not.toBeNull();
  pushUndoOp(op!);
  expect(takeRedoOp()).toEqual({ kind: "move", pairs: [{ from: "/b/1", to: "/a/1" }] });
});

test("the stack is capped, and it is the OLDEST op that falls off", () => {
  for (let i = 0; i < UNDO_CAP + 5; i++) {
    recordFsOp({ kind: "rename", pairs: [{ from: `/w/${i}`, to: `/w/${i}-x` }] });
  }
  const seen: string[] = [];
  for (;;) {
    const op = takeUndoOp();
    if (!op) break;
    seen.push(op.pairs[0].from);
  }
  expect(seen.length).toBe(UNDO_CAP);
  // Newest first, and the five oldest are gone rather than the five newest.
  expect(seen[0]).toBe(`/w/${UNDO_CAP + 4}`);
  expect(seen).not.toContain("/w/0");
});

test("applying an op renames each pair to the EXACT recorded path", async () => {
  // No dedupe on the way back. If the original move landed as "report copy.csv"
  // because the name was taken, the inverse still asks for the original
  // "report.csv" — restoring to a deduped name is not an undo.
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/report copy.csv", to: "/a/report.csv" },
      { from: "/b/y.md", to: "/a/y.md" },
    ],
  });
  expect(renames()).toEqual([
    ["/b/report copy.csv", "/a/report.csv"],
    ["/b/y.md", "/a/y.md"],
  ]);
  expect(report.failed).toBeNull();
  expect(report.done.length).toBe(2);
});

test("applying never asks for an overwrite — a taken name must 409", async () => {
  await applyFsOp({ kind: "rename", pairs: [{ from: "/w/b.md", to: "/w/a.md" }] });
  const rename = posts.find((p) => p.url === "/api/fs/rename");
  expect((rename!.body as { overwrite?: boolean }).overwrite).toBe(false);
});

test("a failure stops the batch and reports what DID land", async () => {
  // Half-undone is the honest outcome: the pairs that moved are named so the
  // caller can offer them as the redo, and the rest are not attempted past an
  // error nobody has seen yet.
  failFor = { src: "/b/2", status: 409, error: "conflict" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  expect(report.done).toEqual([{ from: "/b/1", to: "/a/1" }]);
  expect(report.failed?.pair).toEqual({ from: "/b/2", to: "/a/2" });
  expect(renames().length).toBe(2); // the third was never attempted
});

test("a vanished source fails the same way — the entry is not retried forever", async () => {
  failFor = { src: "/b/gone.md", status: 404, error: "no such file or directory" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [{ from: "/b/gone.md", to: "/a/gone.md" }],
  });
  expect(report.done).toEqual([]);
  expect((report.failed?.error as { status?: number }).status).toBe(404);
});
