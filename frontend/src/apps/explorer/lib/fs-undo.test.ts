// The undo stack for RELOCATIONS, and the arithmetic of inverting one.
//
// Everything here is about the two properties that make undo trustworthy: the
// inverse is the exact path the entry came from (never a deduped one), and an
// entry that cannot be undone leaves the stack rather than sitting at the top
// failing forever.
import { beforeEach, expect, test } from "bun:test";

// The apply path renames through the api layer, and the toast wording reaches
// fs-actions and from there the router, which reads `location` at module scope —
// so both stubs precede the (therefore dynamic) import, the same trade
// fs-move.test.ts makes rather than carrying a DOM.
(globalThis as { location?: unknown }).location = new URL("http://x/");
type Req = { url: string; body: { src?: string; dst?: string } };
const posts: Req[] = [];
// Which renames must fail, and how. A LIST, so one batch can hold several
// failures — the property "every independent failure is reported" is only
// observable within a single applyFsOp call, and a version of this that took one
// rule at a time made that test pass against an unconditional break.
//
//   src      the source path to refuse, or "*" for all of them
//   status   the HTTP status; omit with `rejects` for a request that never
//            answers at all (a dropped connection), which is the only way to
//            reach the status-less branch of isPerPathRefusal / blamedPath
//   error    the server's `error` string, or the thrown Error's message
type FailRule = { src: string; status?: number; error: string; rejects?: boolean };
let failRules: FailRule[] = [];

(globalThis as { fetch?: unknown }).fetch = ((url: string, init?: { body?: string }) => {
  const body = init?.body ? JSON.parse(init.body) : {};
  posts.push({ url, body });
  const rule = failRules.find((r) => r.src === "*" || r.src === body.src);
  if (rule) {
    // No status anywhere on the error — api.ts's httpError always attaches one,
    // so a plain rejection is what a network failure looks like to a caller.
    if (rule.rejects) return Promise.reject(new Error(rule.error));
    return Promise.resolve({
      ok: false,
      status: rule.status,
      json: () => Promise.resolve({ error: rule.error }),
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
  relocationToast,
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
  failRules = [];
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
  failRules = [{ src: "/b/2", status: 409, error: "conflict" }];
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

test("every INDEPENDENT failure in ONE batch is reported, not just the first", async () => {
  // Two names already taken in the destination: unrelated verdicts about two
  // paths, both attempted and both reported, with the pair between them still
  // restored. It has to be one batch — asserting one failure per single-pair call
  // is what an unconditional `break` also passes, which is how this test managed
  // to carry that name without testing it.
  failRules = [
    { src: "/b/1", status: 409, error: "conflict" },
    { src: "/b/3", status: 409, error: "conflict" },
  ];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  expect(report.failed.map((f) => f.pair)).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/3", to: "/a/3" },
  ]);
  expect(report.done).toEqual([{ from: "/b/2", to: "/a/2" }]);
  expect(report.pending).toEqual([]);
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
  failRules = [{ src: "*", status: 403, error: "readonly" }];
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
  // The pair that FAILED is retryable too, at the FRONT of the set. It was not
  // refused for being that path — the destination is read-only, which is just as
  // true of the pairs behind it — so leaving it out would restore everything else
  // on the next attempt and silently abandon that one. Front, not appended,
  // because the retry re-applies these in order.
  expect(report.pending).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/2", to: "/a/2" },
    { from: "/b/3", to: "/a/3" },
  ]);
  // ONE request, not three.
  expect(renames().length).toBe(1);
});

test("NO PAIR IS EVER ORPHANED — done and pending cover the whole op", () => {
  // The invariant behind both branches, stated once. A pair is either restored
  // (and so on the opposite stack) or handed back for a retry; the only pairs that
  // leave both stacks are per-path refusals, which are verdicts about those paths
  // and are named in the toast.
  const pairs = [
    { from: "/b/1", to: "/a/1" },
    { from: "/b/2", to: "/a/2" },
    { from: "/b/3", to: "/a/3" },
  ];
  return (async () => {
    failRules = [{ src: "*", status: 403, error: "readonly" }];
    const systemic = await applyFsOp({ kind: "move", pairs });
    expect([...systemic.done, ...systemic.pending]).toEqual(pairs);
    // A per-path refusal is the documented exception: that pair is dropped on
    // purpose, and everything else still runs.
    failRules = [{ src: "/b/2", status: 409, error: "conflict" }];
    const perPath = await applyFsOp({ kind: "move", pairs });
    expect([...perPath.done, ...perPath.pending, ...perPath.failed.map((f) => f.pair)]).toHaveLength(
      pairs.length
    );
  })();
});

test("A FIXED CAUSE RESTORES EVERYTHING, INCLUDING THE PAIR THAT FAILED", async () => {
  // End to end, the way the caller drives it (listing/useFileOps' runRelocation):
  // take the op, apply its inverse, and put what was not restored back on the
  // stack it came from. Then the user fixes the read-only mount and presses undo
  // again — and every original pair must be back where it started, with none left
  // behind. The failed pair being dropped is invisible in a single round; it shows
  // up here, as one file that never comes home.
  const pairs = [
    { from: "/src/1.md", to: "/dst/1.md" },
    { from: "/src/2.md", to: "/dst/2.md" },
    { from: "/src/3.md", to: "/dst/3.md" },
  ];
  recordFsOp({ kind: "move", pairs });

  // Round one: the destination refuses everything.
  failRules = [{ src: "*", status: 403, error: "readonly" }];
  const first = takeUndoOp()!;
  const r1 = await applyFsOp(invertFsOp(first));
  if (r1.pending.length) {
    pushUndoOp(invertFsOp({ kind: first.kind, pairs: r1.pending }), fsUndoEpoch());
  }

  // The user fixes the cause and presses undo again.
  failRules = [];
  const second = takeUndoOp();
  expect(second).not.toBeNull();
  const r2 = await applyFsOp(invertFsOp(second!));
  expect(r2.failed).toEqual([]);
  expect(r2.pending).toEqual([]);

  // One refused attempt, then the whole set reversed in order — the inverse runs
  // last-pair-first, so 3, 2, 1. Nothing is left on either stack pretending
  // otherwise.
  expect(renames()).toEqual([
    ["/dst/3.md", "/src/3.md"], // round one, refused by the read-only mount
    ["/dst/3.md", "/src/3.md"], // round two: the refused pair, retried FIRST
    ["/dst/2.md", "/src/2.md"],
    ["/dst/1.md", "/src/1.md"],
  ]);
  const restored = new Set(r1.done.concat(r2.done).map((p) => p.to));
  expect([...restored].sort()).toEqual(["/src/1.md", "/src/2.md", "/src/3.md"]);
  expect(takeUndoOp()).toBeNull();
});

test("THE TOAST'S RETRY COUNT COVERS THE REFUSED PAIR AS WELL", async () => {
  // The count is the number going back on the stack, so it includes the pair the
  // sentence just named. Counting only the untried ones was short by one and made
  // the invitation to retry partly false: the user fixes the mount, presses undo,
  // and gets everything except the file the message named.
  failRules = [{ src: "*", status: 403, error: "readonly" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  const { msg, tone } = relocationToast("undo", "move", report);
  expect(tone).toBe("error");
  expect(msg).toContain("3 items left in place — undo again to retry.");
  // Nothing was restored, so it must not claim to have undone part of anything.
  expect(msg).not.toContain("Undid part");
});

test("the toast reads as a plain success only when nothing refused", async () => {
  failRules = [];
  const report = await applyFsOp({ kind: "rename", pairs: [{ from: "/w/b.md", to: "/w/a.md" }] });
  expect(relocationToast("undo", "rename", report)).toEqual({
    msg: "Undid the rename.",
    tone: "info",
  });
  expect(relocationToast("redo", "move", report).msg).toBe("Redid the move.");
});

test("a partial result says so, names the blamed path, and counts the rest", async () => {
  // Two per-path refusals in one batch: one named with its reason, one counted,
  // no `pending` (so no retry invitation), and the successful pair acknowledged.
  failRules = [
    { src: "/b/1", status: 404, error: "no such file or directory" },
    { src: "/b/3", status: 409, error: "conflict" },
  ];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  const { msg } = relocationToast("undo", "move", report);
  expect(msg).toStartWith("Undid part of the move. ");
  // A 404 blames the SOURCE — the path that has gone missing.
  expect(msg).toContain('"1"');
  expect(msg).toContain("(and 1 more)");
  expect(msg).not.toContain("left in place");
});

test("a per-path refusal never sets `pending` — the batch runs to the end", async () => {
  failRules = [{ src: "/b/2", status: 409, error: "conflict" }];
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

test("a 404 mid-batch presses on — a vanished source is one path's problem", async () => {
  // The 404 arm of isPerPathRefusal, which only a MULTI-pair batch can observe:
  // in a single-pair one the batch is over either way and `pending` is empty
  // whichever branch was taken.
  failRules = [{ src: "/b/2", status: 404, error: "no such file or directory" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
      { from: "/b/3", to: "/a/3" },
    ],
  });
  expect(report.pending).toEqual([]);
  expect(report.done).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/3", to: "/a/3" },
  ]);
  expect(renames().length).toBe(3);
});

test("a refusal with NO status is read from its message", async () => {
  // api.ts attaches a status to every HTTP error, so the status-less branch is
  // reached only by a request that never answered — a dropped connection, an
  // unparsable reply. The server's own words are then the only signal, and "no
  // such file" still means one path is missing rather than the machine refusing.
  failRules = [{ src: "/b/2", rejects: true, error: "no such file or directory" }];
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

test("a STATUS decides on its own — the message is not consulted behind it", async () => {
  // The `typeof status === "number"` early return. A 500 that happens to say
  // "conflict" is still the server failing, not a name collision: the status is
  // the authoritative reading whenever there is one, and the message is only the
  // fallback for having none. Without that line this would fall through to the
  // message match and be treated as a per-path verdict, pressing on through a
  // batch that cannot succeed.
  failRules = [{ src: "*", status: 500, error: "conflict while writing" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
    ],
  });
  expect(report.pending).toHaveLength(2); // systemic: bailed, both retryable
  expect(renames().length).toBe(1);
});

test("a status-less refusal naming a CONFLICT is one path's problem", async () => {
  // The `msg.includes("conflict")` arm, which only a rejected request can reach:
  // the destination name is taken, which says nothing about the next pair, so the
  // batch presses on exactly as it does for a 409 with a status.
  failRules = [{ src: "/b/2", rejects: true, error: "conflict" }];
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
  expect(renames().length).toBe(3);
});

test("a status-less refusal that names NEITHER case is systemic", async () => {
  failRules = [{ src: "*", rejects: true, error: "Failed to fetch" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
    ],
  });
  // Both retryable, the refused one first (see applyFsOp's slice).
  expect(report.pending).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/2", to: "/a/2" },
  ]);
});

test("an unrecognised error is treated as systemic — it is not about a path", async () => {
  // A 500, a dropped connection, an unparsable reply: nothing there says the NEXT
  // path would fare better, so the safe reading is "the environment is refusing".
  failRules = [{ src: "*", status: 500, error: "internal error" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [
      { from: "/b/1", to: "/a/1" },
      { from: "/b/2", to: "/a/2" },
    ],
  });
  expect(report.failed.length).toBe(1);
  expect(report.pending).toEqual([
    { from: "/b/1", to: "/a/1" },
    { from: "/b/2", to: "/a/2" },
  ]);
});

test("the blamed path is the one the error is ABOUT, not always the destination", () => {
  const pair = { from: "/b/report copy.csv", to: "/a/report.csv" };
  const err = (status: number, message: string) => Object.assign(new Error(message), { status });
  // 404: the thing that is missing is where the entry sits NOW. Naming the
  // destination reported `"report.csv" no longer exists` — a path that had not
  // existed since the move, about to be recreated — and never named the deduped
  // path that had actually disappeared.
  expect(blamedPath(pair, err(404, "no such file or directory"))).toBe("/b/report copy.csv");
  // The STATUS decides it, not the wording. A 404 whose message says something
  // else entirely — which is the realistic server shape, since the wire text
  // varies — must still blame the source. (With only the message-matching arm
  // under test, deleting the `status === 404` check changed nothing.)
  expect(blamedPath(pair, err(404, "not found"))).toBe("/b/report copy.csv");
  // 409 and everything else are about where it is GOING.
  expect(blamedPath(pair, err(409, "conflict"))).toBe("/a/report.csv");
  expect(blamedPath(pair, err(403, "readonly"))).toBe("/a/report.csv");
  // A thrown non-HttpError still gets read, since the server's words are the
  // only signal there.
  expect(blamedPath(pair, new Error("no such file or directory"))).toBe("/b/report copy.csv");
});

test("a vanished source fails the same way — the entry is not retried forever", async () => {
  failRules = [{ src: "/b/gone.md", status: 404, error: "no such file or directory" }];
  const report = await applyFsOp({
    kind: "move",
    pairs: [{ from: "/b/gone.md", to: "/a/gone.md" }],
  });
  expect(report.done).toEqual([]);
  expect((report.failed[0].error as { status?: number }).status).toBe(404);
});
