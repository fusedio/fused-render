// Claude snapshots: the file-history checkpoint chain (SPEC §34, T:18641-19175,
// inventory 05 §F).
//
// This is Claude Code's OWN data — the very edit history the feature exists to
// protect — so the store stays strictly read-only and the one write goes through
// a two-call contract: `snapshot_plan` DESCRIBES (diff, counts, whether a copy
// of the current bytes will be kept) and `snapshot_revert` only ever applies the
// id the plan handed back. The page never names a version to the write itself.
//
// The reads decline both knobs (`deltas: "0"`, no enrichment) for the reason
// T measured: on a 453 KB file with 12 checkpoints the whole read is 292 ms, of
// which `difflib` inside `file_history._delta` is 290 — two numbers per row.
// Without them the read lands in ~6 ms, which is why the list is simply there
// rather than behind a door. The undiffed timeline softens its counts to NET
// lines, which is why `snapDeltaLabel` renders them with a tilde.
import { statPath } from "@platform/lib/api";
import { runAgent } from "./agent";
import type {
  SnapshotPlanOk,
  SnapshotPlanResponse,
  SnapshotRevertResponse,
  SnapshotVersion,
  SnapshotsTimeline,
} from "./types";

/** One contiguous run of checkpoints from one chat.
 *
 *  Version numbers RESTART in every session (semantic 4 / SPEC FH-4): the store
 *  is `file-history/<sessionId>/<hash>@vN`, so five chats that edited this file
 *  give five chains each beginning at v1, and a flat merged list shows "v2"
 *  three times and reads as duplicate rows. */
export interface SnapRun {
  session: string;
  versions: SnapshotVersion[];
}

/** CONTIGUOUS RUNS, not a `group by session`. The row order is load-bearing —
 *  `_locate` walks the merged timeline positionally to decide where a revert
 *  lands — so this only ever inserts boundaries and never reorders, merges or
 *  re-sorts. The visible consequence is deliberate: a session that edited the
 *  file, went away and came back gets TWO headings, which is what happened
 *  (T:18827-18845). */
export function snapRuns(versions: readonly SnapshotVersion[]): SnapRun[] {
  const runs: SnapRun[] = [];
  for (const v of versions) {
    const open = runs[runs.length - 1];
    if (open && open.session === v.session) open.versions.push(v);
    else runs.push({ session: v.session, versions: [v] });
  }
  return runs;
}

/** One piece of a delta label: its own ink class, or none. */
export interface SnapDeltaPart {
  text: string;
  tone: "plus" | "minus" | "plain";
}

/**
 * What restoring a version would do to the file AS IT IS NOW (T:18672-18709).
 *
 * TWO SHAPES, because `exact: false` is not a hedged version of the same
 * answer. An exact delta is a PAIR — difflib counted lines going in and lines
 * going out, and both are real. An inexact one is a NET, which is a single
 * number by construction (`_delta`'s cheap branch is `max(0, ver - cur)`
 * against `max(0, cur - ver)`, so at most one side can be non-zero). Printing
 * that as a pair gave a column of "~+0 -43" down every row of a file that has
 * only grown, where the "+0" is not a measurement but the shape of the
 * arithmetic — so the net renders as ONE signed term, and the tilde still says
 * it is not a diff.
 */
export function snapDeltaLabel(v: SnapshotVersion): SnapDeltaPart[] {
  if (!v.existed) return [{ text: "did not exist", tone: "minus" }];
  if (v.added == null || v.removed == null)
    return [{ text: "binary", tone: "plain" }];
  if (!v.differs) return [{ text: "on disk now", tone: "plain" }];
  if (!v.exact) {
    // Same line COUNT, different bytes — an edit that replaced as many lines as
    // it removed, which is most edits. The net has nothing to report, so the
    // honest thing is the one fact it does establish.
    if (!v.added && !v.removed) return [{ text: "changed", tone: "plain" }];
    if (!v.removed) return [{ text: `~+${v.added}`, tone: "plus" }];
    if (!v.added) return [{ text: `~−${v.removed}`, tone: "minus" }];
  }
  return [
    { text: `${v.exact ? "" : "~"}+${v.added}`, tone: "plus" },
    { text: " ", tone: "plain" },
    { text: `−${v.removed}`, tone: "minus" },
  ];
}

/** T:18711 `snapAgo` — its own clock, coarser than the chat rows' `ago`: a
 *  checkpoint's minute does not matter, and "time unknown" is a real answer
 *  because the store can hand back a record with no mtime. */
export function snapAgo(sec: number, now: number = Date.now()): string {
  if (!sec) return "time unknown";
  const d = Math.max(0, now / 1000 - sec);
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  if (d < 172800) return "yesterday";
  if (d < 86400 * 30) return `${Math.round(d / 86400)}d ago`;
  return new Date(sec * 1000).toLocaleDateString();
}

/** The one number a row can wear. A did-not-exist checkpoint IS a numbered
 *  version of the chain (v1, the creation boundary), so it keeps its number;
 *  the dash is only for a record that carried no usable one, because "v0" would
 *  invent one the store never wrote (T:18789-18795). */
export function snapVersionLabel(v: SnapshotVersion): string {
  return v.version >= 1 ? `v${v.version}` : "—";
}

/** FILES ONLY: the store keys on one absolute file path, so a folder has no
 *  chain and `agent.py` refuses one. T gates on the page's own `targetNoun`;
 *  native has no such page-level fact on the landing view, so the gate is the
 *  stat the shell already caches — same verdict, no new prop. A stat that fails
 *  is NOT a file (no panel), because a panel whose read is guaranteed to be
 *  refused is worse than an absent one. */
const fileness = new Map<string, boolean>();

export async function isFileTarget(file: string | null): Promise<boolean> {
  if (!file) return false;
  const known = fileness.get(file);
  if (known !== undefined) return known;
  try {
    const st = await statPath(file);
    const yes = !st.is_dir;
    fileness.set(file, yes);
    return yes;
  } catch {
    // Not cached: a failed stat is not an answer about the target.
    return false;
  }
}

/** Test seam. */
export function resetSnapshotTargetCacheForTests(): void {
  fileness.clear();
}

/** T:19124 `loadSnapshots`. Neither knob — see the module note. */
export async function loadSnapshots(
  agentDir: string,
  file: string,
): Promise<SnapshotsTimeline> {
  const out = await runAgent(
    agentDir,
    "snapshots",
    { file, deltas: "0" },
    { key: null },
  );
  if ("error" in out && out.error) throw new Error(out.error);
  return out as SnapshotsTimeline;
}

/** T:18901 `snapExpand`'s read. Fetched fresh on EVERY expansion and never
 *  cached: it is a statement about the file as it is right now, and a stale one
 *  is exactly how a user confirms one diff and gets a different one. */
export function snapshotPlan(
  agentDir: string,
  file: string,
  versionId: string,
): Promise<SnapshotPlanResponse> {
  return runAgent(
    agentDir,
    "snapshot_plan",
    { file, version_id: versionId },
    { key: null },
  );
}

/** T:19000 `snapGoBack`. Handed the id the PLAN returned and nothing else —
 *  the whole of the two-call contract. `confirm_unique` is sent only for the
 *  case the user was actually shown the loss for: a token passed on every call
 *  is a token nobody reads. */
export function snapshotRevert(
  agentDir: string,
  file: string,
  plan: SnapshotPlanOk,
): Promise<SnapshotRevertResponse> {
  return runAgent(
    agentDir,
    "snapshot_revert",
    {
      file,
      version_id: plan.id,
      confirm_unique: plan.unique_current ? "1" : "",
    },
    { key: null },
  );
}
