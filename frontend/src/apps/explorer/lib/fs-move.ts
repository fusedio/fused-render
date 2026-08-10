// Moving entries into a folder — the ONE implementation of it, shared by every
// gesture that means "put these there".
//
// There are two such gestures now (a drag onto a folder row / the listing
// background, and a drag onto a sidebar bookmark), and they live in different
// component trees, which is exactly why this is a module and not a method on
// the listing's file-ops hook. A second move path would be a second set of
// answers to the questions below — name collisions, entries already in the
// target, a clipboard still pointing at the old location — and the one place
// those are already answered is the cut-and-paste flow, whose rules this
// deliberately mirrors:
//
//   • the DESTINATION NAME keeps the entry's own name when it is free in the
//     target and falls back to the first free "… copy[/ n]" when it is taken
//     (freePastePath) — Finder's keep-both, instead of a bare 409 or an
//     overwrite prompt nobody asked for. This is the "conflict dialog" the
//     explorer has: it resolves the conflict rather than posing it, which is
//     also why a drag needs no confirmation UI of its own;
//   • DESCENDANTS of a dragged folder are dropped from the batch — moving the
//     folder already takes them, and the second call would 404 on a path its
//     own parent just moved (pruneDescendantPaths);
//   • an entry ALREADY IN the target is skipped rather than moved onto itself;
//   • the CLIPBOARD is repointed at the new path, so a pending cut/copy of
//     something that just moved doesn't paste from a dead source.
//
// Sequential, not parallel, for freePastePath's sake: each call resolves a free
// name against a listing, and two in flight would both pick the same one.
import { renameEntry, statPath } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { pushToast } from "@platform/lib/toast";
import {
  freePastePath,
  friendlyFsError,
  join,
  normDir,
  pruneDescendantPaths,
  remapClipboardPath,
} from "@apps/explorer/lib/fs-actions";

// What a move of `paths` into `targetDir` will actually do: `move` is the
// entries that change folder, `skip` the ones already there (and, silently, the
// descendants of a dragged folder, which its own move carries). Pure, so the
// arithmetic of a batch is testable without a server.
export function movePlan(
  paths: readonly string[],
  targetDir: string,
): { move: string[]; skip: string[] } {
  const dir = normDir(targetDir);
  const move: string[] = [];
  const skip: string[] = [];
  for (const p of pruneDescendantPaths([...paths])) {
    if (join(dir, basename(p)) === p) skip.push(p);
    else move.push(p);
  }
  return { move, skip };
}

export interface MoveReport {
  // Destination paths, in the order they were written. The last one is what a
  // caller re-anchors its selection onto.
  moved: string[];
  // The first failure, if any. The batch stops there — the rest of the entries
  // are left where they are rather than half-moved past an error nobody saw.
  failed: { path: string; error: unknown } | null;
}

// Move `paths` into `targetDir`, reporting rather than throwing: both callers
// want the same three things out of a partial batch (refresh what did move,
// say what didn't, re-anchor on the last success), and a throw would make each
// of them reconstruct that from a catch.
//
// The error toast is raised HERE, so a failed drag explains itself the same way
// wherever it was dropped. Success is silent by default — the listing refreshes
// under the cursor and that is the confirmation — but a caller whose target is
// OFF SCREEN (a sidebar bookmark) asks for `announce`, because a move you
// cannot see needs to be told.
export async function moveEntriesInto(
  paths: readonly string[],
  targetDir: string,
  { announce = false }: { announce?: boolean } = {},
): Promise<MoveReport> {
  const dir = normDir(targetDir);
  const { move } = movePlan(paths, dir);
  const report: MoveReport = { moved: [], failed: null };
  for (const src of move) {
    try {
      const { is_dir } = await statPath(src);
      const dst = await freePastePath(dir, basename(src), is_dir);
      await renameEntry(src, dst);
      remapClipboardPath(src, dst);
      report.moved.push(dst);
    } catch (e) {
      report.failed = { path: src, error: e };
      break;
    }
  }
  if (report.failed) {
    pushToast({
      msg: friendlyFsError(report.failed.error, {
        verb: "move",
        name: basename(report.failed.path),
      }),
      tone: "error",
    });
  } else if (announce && report.moved.length) {
    const what = report.moved.length === 1 ? basename(report.moved[0]) : `${report.moved.length} items`;
    pushToast({ msg: `Moved ${what} to ${basename(dir)}`, tone: "info" });
  }
  return report;
}
