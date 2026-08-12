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
// Which rename must fail, and with what — how a 404 (source gone), a 409
// (something is in the way now) and a 403 (a read-only destination, which fails
// every pair) are driven. "*" fails all of them.
let failFor: { src: string; status: number; error: string } | null = null;

(globalThis as { fetch?: unknown }).fetch = ((url: string, init?: { body?: string }) => {
  const body = init?.body ? JSON.parse(init.body) : {};
  posts.push({ url, body });
  if (failFor && (failFor.src === "*" || body.src === failFor.src)) {
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
  beginFsUndo,
  blamedPath,
  endFsUndo,
  fsUndoEpoch,
  invertFsOp,
  isFsUndoInFlight,
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
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/x.md", to: "/a/x.md" }] }, fsUndoEpoch());
  recordFsOp({ kind: "rename", pairs: [{ from: "/a/y.md", to: "/a/z.md" }] });
  expect(takeRedoOp()).toBeNull();
});

test("A NEW OP DURING AN UNDO KILLS THAT UNDO'S REDO ENTRY", () => {
  // The race the epoch exists for. An undo's renames take real time, and the
  // redo entry is only pushed when they finish — so a relocation started
  // meanwhile (a drag onto a folder) cleared the redo stack BEFORE the push,
  // and the push then resurrected an entry recorded against a filesystem that
  // had since changed. Shift+Cmd+Z would replay the whole batch.
  //
  // The undo reads the epoch when it STARTS and offers it back with the push;
  // a recordFsOp in between makes the push a no-op, which is the same outcome
  // recordFsOp's own invariant asks for.
  const started = fsUndoEpoch();
  recordFsOp({ kind: "move", pairs: [{ from: "/a/x.md", to: "/b/x.md" }] }); // the racer
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/y.md", to: "/a/y.md" }] }, started);
  expect(takeRedoOp()).toBeNull();
});

test("pushing back onto a stack after a redo keeps the OTHER stack", () => {
  // pushUndoOp is not recordFsOp: a redo puts the op back on the undo stack
  // without declaring a new future, so the redo entries below it survive.
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/1", to: "/a/1" }] }, fsUndoEpoch());
  pushRedoOp({ kind: "move", pairs: [{ from: "/b/2", to: "/a/2" }] }, fsUndoEpoch());
  const op = takeRedoOp();
  expect(op).not.toBeNull();
  pushUndoOp(op!, fsUndoEpoch());
  expect(takeRedoOp()).toEqual({ kind: "move", pairs: [{ from: "/b/1", to: "/a/1" }] });
});

test("a stale epoch does NOT drop an undo-stack push — that would lose work", () => {
  // Asymmetric on purpose. The redo stack is a claim about a future that a new
  // op invalidates; the undo stack is a record of relocations that all really
  // happened, and dropping one because something else happened first would make
  // a real move permanently un-undoable.
  const started = fsUndoEpoch();
  recordFsOp({ kind: "move", pairs: [{ from: "/a/new.md", to: "/b/new.md" }] });
  pushUndoOp({ kind: "rename", pairs: [{ from: "/w/a", to: "/w/b" }] }, started);
  expect(takeUndoOp()).toEqual({ kind: "rename", pairs: [{ from: "/w/a", to: "/w/b" }] });
});

test("the in-flight guard lives with the stacks, not with a component", () => {
  // It used to be a useRef in useFileOps, which a navigation mid-gesture
  // remounted to false — so a second Cmd+Z ran concurrently with the first and
  // two batches of renames over possibly-nested paths interleaved, breaking the
  // sequential ordering applyFsOp depends on. Module-level, like the stacks it
  // guards, it survives the remount.
  expect(isFsUndoInFlight()).toBe(false);
  expect(beginFsUndo()).toBe(true);
  expect(isFsUndoInFlight()).toBe(true);
  expect(beginFsUndo()).toBe(false); // the second gesture is refused
  endFsUndo();
  expect(isFsUndoInFlight()).toBe(false);
  expect(beginFsUndo()).toBe(true);
  endFsUndo();
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
  expect(report.failed).toEqual([]);
  expect(report.done.length).toBe(2);
});

test("applying never asks for an overwrite — a taken name must 409", async () => {
  await applyFsOp({ kind: "rename", pairs: [{ from: "/w/b.md", to: "/w/a.md" }] });
  const rename = posts.find((p) => p.url === "/api/fs/rename");
  expect((rename!.body as { overwrite?: boolean }).overwrite).toBe(false);
});

test("ONE PAIR'S FAILURE DOES NOT ABANDON THE REST OF THE BATCH", async () => {
  // The property that keeps an undo from leaving a worse state than it found.
  // Stopping at the 409 would restore pair 1, consume the entry, and leave pairs
  // 3 and 4 still moved with nothing on either stack naming them — orphaned by
  // the very gesture meant to put them back. Every pair is attempted, so each
  // one ends up either restored (and on the opposite stack) or reported.
  failFor = { src: "/b/2", status: 409, error: "conflict" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
      { from: "/b/4", to: "/a/4" },
    ],
  });
  expect(report.done).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/3", to: "/a/3" },
    { from: "/b/4", to: "/a/4" },
  ]);
  expect(report.failed.map((f) => f.pair)).toEqual([{ from: "/b/2", to: "/a/2" }]);
  expect(report.pending).toEqual([]);
  // All four attempted — nothing was skipped past the error.
  expect(renames().length).toBe(4);
});

test("every INDEPENDENT failure is reported, not just the first", async () => {
  // Two different names taken in the destination: unrelated verdicts about two
  // paths, so both are attempted and both reported.
  failFor = { src: "/b/1", status: 409, error: "conflict" };
  const first = await applyFsOp({ kind: "move", pairs: [{ from: "/b/1", to: "/a/1" }] });
  failFor = { src: "/b/2", status: 409, error: "conflict" };
  const second = await applyFsOp({ kind: "move", pairs: [{ from: "/b/2", to: "/a/2" }] });
  expect(first.failed.length).toBe(1);
  expect(second.failed.length).toBe(1);
});

test("A SYSTEMIC REFUSAL BAILS OUT INSTEAD OF FIRING THE WHOLE BATCH", async () => {
  // A read-only destination refuses every pair for the same reason, and the
  // reason has nothing to do with any path. Pressing on would fire one doomed
  // request per pair — a 3,000-pair cut-paste undone into a read-only mount meant
  // 3,000 sequential 403s with no toast and no progress, and (because the
  // in-flight guard only clears at the end) undo looked dead for minutes.
  //
  // So: one attempt, then stop, and hand back the pairs that were NOT tried so
  // the caller can leave them undoable. This is the opposite call from the 404 /
  // 409 case above, and the distinction is whether the error is a verdict about
  // a PATH or about the environment.
  failFor = { src: "*", status: 403, error: "readonly" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  expect(report.done).toEqual([]);
  expect(report.failed.length).toBe(1);
  expect(report.pending).toEqual([
    { from: "/b/2", to: "/a/2" },
    { from: "/b/3", to: "/a/3" },
  ]);
  // ONE request, not three.
  expect(renames().length).toBe(1);
});

test("a per-path refusal never sets `pending` — the batch runs to the end", async () => {
  failFor = { src: "/b/2", status: 409, error: "conflict" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  expect(report.pending).toEqual([]);
  expect(report.done.length).toBe(2);
});

test("an unrecognised error is treated as systemic — it is not about a path", async () => {
  // A 500, a dropped connection, an unparsable reply: nothing there says the NEXT
  // path would fare better, so the safe reading is "the environment is refusing".
  failFor = { src: "*", status: 500, error: "internal error" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
    ],
  });
  expect(report.failed.length).toBe(1);
  expect(report.pending).toEqual([{ from: "/b/2", to: "/a/2" }]);
});

test("the blamed path is the one the error is ABOUT, not always the destination", () => {
  const pair = { from: "/b/report copy.csv", to: "/a/report.csv" };
  const err = (status: number, message: string) => Object.assign(new Error(message), { status });
  // 404: the thing that is missing is where the entry sits NOW. Naming the
  // destination reported `"report.csv" no longer exists` — a path that had not
  // existed since the move, about to be recreated — and never named the deduped
  // path that had actually disappeared.
  expect(blamedPath(pair, err(404, "no such file or directory"))).toBe("/b/report copy.csv");
  // 409 and everything else are about where it is GOING.
  expect(blamedPath(pair, err(409, "conflict"))).toBe("/a/report.csv");
  expect(blamedPath(pair, err(403, "readonly"))).toBe("/a/report.csv");
  // A thrown non-HttpError still gets read, since the server's words are the
  // only signal there.
  expect(blamedPath(pair, new Error("no such file or directory"))).toBe("/b/report copy.csv");
});

test("a vanished source fails the same way — the entry is not retried forever", async () => {
  failFor = { src: "/b/gone.md", status: 404, error: "no such file or directory" };
  const report = await applyFsOp({
    kind: "move",
    pairs: [{ from: "/b/gone.md", to: "/a/gone.md" }],
  });
  expect(report.done).toEqual([]);
  expect((report.failed[0].error as { status?: number }).status).toBe(404);
});
