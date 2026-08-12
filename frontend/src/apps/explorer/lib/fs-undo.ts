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
import { basename } from "@platform/lib/format";
import { friendlyFsError } from "@apps/explorer/lib/fs-actions";

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

// Bumped by every NEW recorded relocation. An undo reads it when it STARTS and
// hands it back when it finishes, so a push can tell whether the filesystem moved
// on underneath it — see pushRedoOp, which is the whole reason this exists.
let epoch = 0;

export function fsUndoEpoch(): number {
  return epoch;
}

// Is an undo or redo running RIGHT NOW? Module-level, next to the stacks it
// protects, and that placement is the fix rather than an implementation detail:
// this used to be a useRef inside useFileOps, which a navigation mid-gesture
// remounted to `false`. A second Cmd+Z then ran concurrently with the first, and
// two batches of renames over possibly-nested paths interleaved — the exact
// sequential ordering applyFsOp says is the only thing keeping a nested batch
// sound. A move can navigate (spring-loading a crumb), so a guard that dies with
// the component is no guard at all.
let inFlight = false;

export function isFsUndoInFlight(): boolean {
  return inFlight;
}

// Claim the undo lane, or refuse. `false` means one is already running; the
// caller must not proceed (and must not call endFsUndo).
export function beginFsUndo(): boolean {
  if (inFlight) return false;
  inFlight = true;
  return true;
}

export function endFsUndo(): void {
  inFlight = false;
}

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
  epoch++;
}

// Put an op back WITHOUT declaring a new future — what an undo and a redo do to
// the opposite stack. Distinct from recordFsOp precisely because it must not
// clear the other side: a redo leaves the rest of the redo stack intact.
//
// `atEpoch` is the epoch the gesture STARTED at, and the two stacks treat a stale
// one differently on purpose:
//
//   • the UNDO stack takes the push regardless (the argument is accepted and
//     ignored, so both directions share one call site). Every op on it describes
//     a relocation that really happened; dropping one because something else
//     happened first would make a real move permanently un-undoable.
//   • the REDO stack does not. A redo entry is a claim about a future, and
//     recordFsOp exists to revoke exactly that claim when the filesystem moves
//     on. Since an undo's renames take real time, a relocation started while one
//     is in flight cleared the redo stack BEFORE the undo pushed onto it — and
//     the push resurrected an entry recorded against a filesystem that had since
//     changed, so Shift+Cmd+Z would replay the whole batch. Comparing epochs is
//     what makes the invariant hold across the await instead of only at the
//     instant of the clear.
export function pushUndoOp(op: FsOp, _atEpoch: number): void {
  push(undoStack, op);
}

export function pushRedoOp(op: FsOp, atEpoch: number): void {
  if (atEpoch !== epoch) return; // a new relocation has revoked this future
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
  inFlight = false;
}

export interface FsOpReport {
  // The pairs that landed, in the order they were written — the caller's entry
  // for the opposite stack, and what it re-anchors the selection onto.
  done: FsPair[];
  // EVERY per-path refusal, not just the first. Each one is dropped from both
  // stacks: it was taken off before the attempt, and a pair that 404s or 409s
  // would sit at the top failing for every later Undo.
  failed: { pair: FsPair; error: unknown }[];
  // Everything a systemic refusal left undone (see applyFsOp): the pair it
  // refused, first, plus the pairs it never got to. Worth keeping, unlike a
  // per-path failure, because the reason is environmental and usually fixable —
  // the caller puts these back on the stack it took the op from, in this order,
  // and the user can undo again once it is fixed. Empty when the batch ran to the
  // end, which is every per-path case.
  pending: FsPair[];
}

// Is this error a verdict about ONE PATH, or about the environment?
//
// Per-path: 404 (that source is gone) and 409 (that destination name is taken).
// Neither says anything about the next pair, so the batch presses on.
//
// Everything else — 403 readonly, a 5xx, a dropped connection, an unparsable
// reply — is a property of the destination or the machine, and the next pair will
// meet it identically. Treating the unrecognised case as systemic is the safe
// default: the cost of being wrong is that a few pairs stay undoable instead of
// being retried now, while the cost of the opposite is thousands of doomed
// sequential requests.
// WHICH HALF OF THE PAIR IS THE ERROR ABOUT? A restore has two paths in it and
// the answer is not always the destination.
//
//   • the source vanished (404) — the thing that is missing is `from`, the path
//     the entry currently sits at. Undoing a rename of a.txt -> b.txt after b.txt
//     was deleted has to say b.txt is gone; naming the destination said `"a.txt"
//     no longer exists`, a path that had not existed since the rename and was
//     about to be recreated. After a deduped move it was worse still — it blamed
//     "report.csv" when "report copy.csv" was the path that had disappeared.
//   • anything else, 409 above all, is about where the entry is GOING: the name
//     is taken, the parent is missing, the target is read-only.
export function blamedPath(pair: FsPair, error: unknown): string {
  const status = (error as { status?: number } | null)?.status;
  const msg = (error instanceof Error ? error.message : String(error)).toLowerCase();
  const missingSource = status === 404 || msg.includes("no such file");
  return missingSource ? pair.from : pair.to;
}

function isPerPathRefusal(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  if (status === 404 || status === 409) return true;
  if (typeof status === "number") return false;
  // No status at all (a thrown non-HttpError): fall back to the message, which is
  // the server's own word for these two cases.
  const msg = (error instanceof Error ? error.message : String(error)).toLowerCase();
  return msg.includes("no such file") || msg.includes("conflict");
}

// The one toast an undo or a redo raises, from what the attempt reported.
//
// Announced at all for the same reason a drop onto a sidebar bookmark is
// (fs-move's `announce`): the entries may well have gone back to a folder that is
// not the one on screen, so a refetch alone would look like nothing happened.
//
// A PARTIAL RESULT MUST NOT READ AS AN ALL-CLEAR. "Undid the move" over a batch
// where two entries refused would be a false success about the two that are still
// where the move left them. So a failure names the first refusal and its reason —
// on the half of the pair the error is actually about (blamedPath) — and counts the
// rest: one sentence rather than a toast per pair, because a read-only destination
// refuses every entry for the same reason and twenty identical toasts would bury
// the one that mattered.
//
// The "left in place" count is `pending`, which INCLUDES the pair that was
// refused. It has to: that pair is going back on the stack with the others, so a
// count that omitted it would be short by one and its invitation to retry would be
// partly false — the user would fix the cause, press undo, and get everything
// except the file the message had just named.
//
// Pure, and separate from the hook that shows it, so the arithmetic is testable
// without a renderer (the wording used to live inline in useFileOps).
export function relocationToast(
  verb: "undo" | "redo",
  kind: FsOp["kind"],
  report: FsOpReport,
): { msg: string; tone: "info" | "error" } {
  const did = verb === "undo" ? "Undid" : "Redid";
  if (!report.failed.length) {
    return { msg: `${did} the ${kind}.`, tone: "info" };
  }
  const [first] = report.failed;
  const rest = report.failed.length - 1;
  const held = report.pending.length;
  return {
    msg:
      (report.done.length ? `${did} part of the ${kind}. ` : "") +
      friendlyFsError(first.error, { verb, name: basename(blamedPath(first.pair, first.error)) }) +
      (rest ? ` (and ${rest} more)` : "") +
      (held ? ` ${held} ${held === 1 ? "item" : "items"} left in place — ${verb} again to retry.` : ""),
    tone: "error",
  };
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
// IT DOES NOT STOP AT THE FIRST PER-PATH FAILURE, and that is a deliberate
// departure
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
// next pair's chances.
//
// BUT A SYSTEMIC REFUSAL ENDS THE BATCH AT ONCE (isPerPathRefusal). There the
// premise fails — the reason is the destination or the machine, not the path, so
// every remaining pair is a request known in advance to fail. There is no bound
// worth relying on either: UNDO_CAP caps the number of OPS on the stack, not the
// pairs within one, and a cut-paste of a few thousand files is a single op. Undone
// into a read-only mount that meant thousands of sequential 403s with nothing on
// screen — and since the in-flight guard only clears at the end, undo looked dead
// for minutes. One attempt, then stop, and the untried pairs come back in
// `pending` so the caller can leave them undoable rather than orphaning them.
//
// Sequential, like every other batch here: these are renames of paths that may
// be nested in each other, and order is the only thing keeping that sound.
export async function applyFsOp(op: FsOp): Promise<FsOpReport> {
  const report: FsOpReport = { done: [], failed: [], pending: [] };
  for (let i = 0; i < op.pairs.length; i++) {
    const pair = op.pairs[i];
    try {
      await renameEntry(pair.from, pair.to);
      report.done.push(pair);
    } catch (error) {
      report.failed.push({ pair, error });
      if (!isPerPathRefusal(error)) {
        // FROM `i`, NOT `i + 1`: the pair that just failed is retryable too. It
        // was not refused for being that path — the destination is read-only, or
        // the machine is — which is exactly as true of the pairs behind it, so
        // handing back only those would restore everything else on the next
        // attempt and silently abandon this one. It goes at the FRONT because the
        // retry re-applies the set in order, and that order is the only thing
        // keeping a batch of nested paths sound.
        //
        // It is therefore in BOTH `failed` and `pending`: `failed` is what the
        // caller SAYS (this is the refusal, and why), `pending` is what the caller
        // KEEPS. The two answer different questions and the same pair can be the
        // answer to both.
        report.pending = op.pairs.slice(i);
        break;
      }
    }
  }
  return report;
}
