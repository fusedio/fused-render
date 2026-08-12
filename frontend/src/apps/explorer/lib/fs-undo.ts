// Undo/redo for RELOCATIONS — the operations that are a rename in both
// directions, and only those.
//
// WHAT IS UNDOABLE, and why the list is this short. A drag-move, a cut-paste and
// a rename all do the same thing at the filesystem: they rename a path. Their
// inverse is another rename, it destroys nothing, and it can be redone by
// renaming again — so undo, redo and undo-again are all the same primitive and
// each one is reproducible.
//
// Everything else the explorer can do is deliberately NOT here:
//
//   copy-paste, duplicate, new file/folder, compress → the inverse is a DELETE.
//     Undo would destroy data on the user's behalf, and a redo after it cannot
//     reproduce what was lost.
//   delete / trash → the inverse is a restore we do not own (the OS Trash), so
//     "undo" would be a promise this module cannot keep.
//
// A stack of two kinds of op is not a limitation to be grown out of; it is the
// reason this can be trusted at all. Adding an asymmetric op means answering
// "what does redo mean after an undo that deleted something", and there is no
// good answer.
//
// AN ENTRY IS DATA, NOT A CLOSURE. Every pair is a pair of absolute paths, and
// nothing in here captures React state. A move can navigate and remount the
// listing mid-gesture (spring-loading a crumb), so a closure recorded before the
// move would be reading a component that no longer exists — the same reason the
// clipboard and the in-flight drag are module-level stores (lib/fs-clipboard,
// listing/drag-drop).
import { renameEntry } from "@platform/lib/api";

export interface FsPair {
  from: string;
  to: string;
}

export interface FsOp {
  // Only for the toast's wording ("Undid the move" / "Undid the rename"). The
  // mechanics are identical — which is exactly why both are here.
  kind: "move" | "rename";
  // The pairs that ACTUALLY LANDED, in the order they were written. Recording
  // the intended batch instead would try to un-move entries that never moved:
  // moveEntriesInto stops at its first failure, so an interrupted batch has
  // fewer pairs than it was asked for, and each pair's `to` is the real
  // destination (freePastePath can turn "report.csv" into "report copy.csv").
  pairs: FsPair[];
}

// How many ops are remembered. Deep enough that undo covers a session's worth
// of reorganising, shallow enough that the oldest entries — whose paths have had
// the most time to go stale — fall off on their own.
export const UNDO_CAP = 50;

const undoStack: FsOp[] = [];
const redoStack: FsOp[] = [];

// The inverse: every pair swapped, and the batch reversed. Reversal is what
// makes an op that renamed A then B come apart as B then A, so a sequence that
// depended on its own order unwinds in the opposite one. Applying it twice is
// the original op again, which is what lets undo and redo share one code path.
export function invertFsOp(op: FsOp): FsOp {
  return {
    kind: op.kind,
    pairs: [...op.pairs].reverse().map((p) => ({ from: p.to, to: p.from })),
  };
}

function push(stack: FsOp[], op: FsOp): void {
  if (!op.pairs.length) return; // an op that moved nothing is not an op
  stack.push(op);
  if (stack.length > UNDO_CAP) stack.splice(0, stack.length - UNDO_CAP);
}

// A NEW user operation. This is the one that drops the redo stack: once the
// filesystem has moved on, redoing something undone earlier would be replaying
// it against a different filesystem than the one it was recorded against.
export function recordFsOp(op: FsOp): void {
  push(undoStack, op);
  redoStack.length = 0;
}

// Put an op back WITHOUT declaring a new future — what an undo and a redo do to
// the opposite stack. Distinct from recordFsOp precisely because it must not
// clear the other side: a redo leaves the rest of the redo stack intact.
export function pushUndoOp(op: FsOp): void {
  push(undoStack, op);
}

export function pushRedoOp(op: FsOp): void {
  push(redoStack, op);
}

// Take the op off the stack BEFORE trying it. A 404 (the entry has since been
// moved or deleted) or a 409 (something else has taken the name) makes that
// entry permanently unusable, and leaving it in place would park a failing op at
// the top of the stack where every later Undo hits it instead of the one the
// user meant.
export function takeUndoOp(): FsOp | null {
  return undoStack.pop() ?? null;
}

export function takeRedoOp(): FsOp | null {
  return redoStack.pop() ?? null;
}

export function canUndo(): boolean {
  return undoStack.length > 0;
}

export function canRedo(): boolean {
  return redoStack.length > 0;
}

// Tests only: a module-level store outlives a test case.
export function resetFsUndo(): void {
  undoStack.length = 0;
  redoStack.length = 0;
}

export interface FsOpReport {
  // The pairs that landed, in the order they were written — the caller's entry
  // for the opposite stack, and what it re-anchors the selection onto.
  done: FsPair[];
  // EVERY pair that refused, not just the first. Each one is dropped from both
  // stacks: it was taken off before the attempt, and a pair that 404s or 409s
  // would sit at the top failing for every later Undo.
  failed: { pair: FsPair; error: unknown }[];
}

// Perform an op: rename `from` to `to` for each pair, in order.
//
// THE DESTINATION IS EXACT, AND freePastePath IS DELIBERATELY NOT USED HERE.
// Everywhere else in the explorer a name collision is resolved by taking the
// first free "… copy" name (fs-move, doPaste) — Finder's keep-both, so a move
// never has to pose a conflict dialog. An undo is the one place where that is
// the wrong answer: the whole promise is "put it back where it was", and
// silently restoring "report.csv" as "report copy.csv" would be a second
// relocation dressed as an undo. So the rename asks for the recorded path with
// overwrite off, and a name that has since been taken comes back as a 409 for
// the caller to say out loud.
//
// IT DOES NOT STOP AT THE FIRST FAILURE, and that is a deliberate departure
// from the batch loops elsewhere (moveEntriesInto, doPaste) that break. The
// reason those break is that a fresh user operation half-applied leaves data
// spread across two places, and stopping early both limits that and leaves the
// whole gesture available to retry.
//
// Neither half of that holds for an inverse. An undo is already the repair, so
// stopping in the middle of one leaves a MORE broken state than finishing it:
// partly restored, with the untried remainder still moved and — because the
// entry was consumed to attempt it — no longer named by anything on either
// stack. It would be orphaned exactly where "half-moved past an error nobody
// saw" is the hazard being guarded against. And retry-the-whole-thing is not
// available: the entry is gone.
//
// Pressing on is also sound in a way it would not be for a move: each pair is an
// independent rename of a distinct path, so one pair's 409 says nothing about the
// next pair's chances. A systemic refusal (a read-only destination) costs one
// request per pair, bounded by UNDO_CAP, and is reported once by the caller.
//
// Sequential, like every other batch here: these are renames of paths that may
// be nested in each other, and order is the only thing keeping that sound.
export async function applyFsOp(op: FsOp): Promise<FsOpReport> {
  const report: FsOpReport = { done: [], failed: [] };
  for (const pair of op.pairs) {
    try {
      await renameEntry(pair.from, pair.to);
      report.done.push(pair);
    } catch (error) {
      report.failed.push({ pair, error });
    }
  }
  return report;
}
