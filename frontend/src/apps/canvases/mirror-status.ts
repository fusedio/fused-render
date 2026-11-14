// What the Canvases page says about the reference canvases — the read-only
// copies of every canvas the account can reach, which a background run keeps
// under ~/Fused/canvas/<env>/ for the coding agent to search
// (fused_render/canvas_mirror.py). Pure: MirrorStatus.tsx renders what this
// decides, and the tests beside this file pin every state.
//
// The rule that shapes all of it: a reader who is not using the reference
// canvases sees nothing. No CLI, signed out, an environment nobody uses, or
// no run finished yet — no chip.
import { timeAgo } from "@platform/lib/format";
import type { LayoutPending, MirrorRunReason, MirrorSummary } from "./api";

export type MirrorChipTone = "running" | "ok" | "warning" | "error" | "muted";

export interface MirrorChip {
  tone: MirrorChipTone;
  text: string;
  /** The hover text: the run's own message where it left one. */
  title: string;
  /** A press starts a run. False while one is in flight, and where a run
   *  could not help (a Fused without the command). */
  canRun: boolean;
}

const ROOT_HINT = "Read-only copies of the canvases you can open, kept for Claude to search.";
const RUN_HINT = "Click to update now.";

/** "340 reference canvases" / "1 reference canvas". */
export function referenceCanvasCount(n: number): string {
  return `${n.toLocaleString("en-US")} reference ${n === 1 ? "canvas" : "canvases"}`;
}

/** "340 reference canvases · updated 12m ago"; the stamp is dropped when
 *  the run left none. */
export function mirroredLine(mirrored: number, finishedAt: number | null): string {
  const ago = timeAgo(finishedAt);
  const count = referenceCanvasCount(mirrored);
  return ago ? `${count} · updated ${ago}` : count;
}

/** "Updating reference canvases… 57 of 340"; no counts until the run has
 *  listed what there is to fetch. */
export function runningLine(done: number, total: number): string {
  const head = "Updating reference canvases…";
  if (!(total > 0)) return head;
  const shown = Math.min(Math.max(done, 0), total);
  return `${head} ${shown.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}`;
}

const joined = (...parts: (string | null | undefined)[]) =>
  parts.filter((p): p is string => !!p).join(" ");

/** The header chip for a mirror summary, or null for no chip at all. */
export function mirrorChip(mirror: MirrorSummary | null | undefined): MirrorChip | null {
  if (!mirror) return null;
  // Said even though no run can start: it is why there are no reference
  // canvases, and the remedy is the reader's.
  if (mirror.state === "unsupported" || mirror.skip_reason === "unsupported") {
    return {
      tone: "muted",
      text: "Reference canvases need a newer Fused",
      title: joined(ROOT_HINT, "The installed Fused does not have the command that fetches them."),
      canRun: false,
    };
  }
  if (mirror.state === "no_cli" || !mirror.eligible) return null;
  switch (mirror.state) {
    case "running":
      return {
        tone: "running",
        text: runningLine(mirror.done, mirror.total),
        title: ROOT_HINT,
        canRun: false,
      };
    case "busy":
      // Another process's run: its progress is not ours to read.
      return {
        tone: "running",
        text: runningLine(0, 0),
        title: joined(ROOT_HINT, "Another Fused window is updating them."),
        canRun: true,
      };
    case "ok":
      return {
        tone: "ok",
        text: mirroredLine(mirror.mirrored, mirror.finished_at),
        title: joined(ROOT_HINT, RUN_HINT),
        canRun: true,
      };
    case "partial":
      return {
        tone: "warning",
        text: mirroredLine(mirror.mirrored, mirror.finished_at),
        title: joined(
          `Some reference canvases could not be updated: ${mirror.error || "the last update was incomplete"}.`,
          RUN_HINT,
        ),
        canRun: true,
      };
    case "auth":
      return {
        tone: "error",
        text: "Sign in again to update reference canvases",
        title: joined(mirror.error ? `${mirror.error}.` : null, "Sign out and back in, then click to retry."),
        canRun: true,
      };
    case "failed":
      return {
        tone: "error",
        text: "Reference canvases could not be updated",
        title: joined(mirror.error ? `${mirror.error}.` : null, "Click to retry."),
        canRun: true,
      };
    default:
      // idle (no run has finished yet), and any state a newer server adds.
      return null;
  }
}

/** The sentence for a refresh the server did not start, or null when that is
 *  no failure: a run is already going, or one starts by itself once the
 *  clone / rename / delete holding it ends. */
export function mirrorRunRefusal(reason: MirrorRunReason | string | null): string | null {
  switch (reason) {
    case null:
    case "running":
    case "held":
      return null;
    case "unsupported":
      return "Reference canvases need a newer Fused.";
    case "no_cli":
      return "Reference canvases could not be updated: the fused CLI is not available.";
    case "signed_out":
      return "Sign in to Fused to update reference canvases.";
    case "not_configured":
      return "Reference canvases could not be updated: the custom environment is not configured.";
    case "env_locked":
      return "Reference canvases could not be updated: the environment is pinned by FUSED_RENDER_WORKBENCH_ENV.";
    case "shutting_down":
      return "Reference canvases could not be updated: the server is shutting down.";
    default:
      return "Reference canvases could not be updated.";
  }
}

export interface LayoutNotice {
  /** The one line that is always on show. */
  summary: string;
  /** Canvases stranded at their old path, by name, with the reason. */
  conflicts: { name: string; why: string }[];
  /** Why the whole move could not run, when it could not. */
  detail: string | null;
}

/** The notice for canvases the move to the new folder layout left behind, or
 *  null. `deferred` (a canvas whose chat is still open) moves by itself and
 *  is not worth a notice. */
export function layoutNotice(layout: LayoutPending | null | undefined): LayoutNotice | null {
  if (!layout) return null;
  const conflicts = Object.entries(layout.conflicts ?? {})
    .map(([name, why]) => ({ name, why }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const detail = layout.error || layout.blocked || null;
  if (conflicts.length === 0 && !detail) return null;
  const n = conflicts.length;
  const summary =
    n === 0
      ? "Existing canvases could not be moved to the new folder layout"
      : `${n} existing ${n === 1 ? "canvas" : "canvases"} could not be moved to the new folder layout`;
  return { summary, conflicts, detail };
}

// How often the page re-reads the mirror status: closely while a run is in
// flight (its counts move per canvas), slowly otherwise — enough to notice a
// scheduled run starting and to keep "updated … ago" honest.
export const MIRROR_POLL_RUNNING_MS = 2000;
export const MIRROR_POLL_IDLE_MS = 30000;

/** The delay before the next status read, or null for none: nothing is read
 *  for an environment where no run would start. */
export function mirrorPollDelay(mirror: MirrorSummary | null | undefined): number | null {
  if (!mirror) return null;
  if (mirror.state === "running") return MIRROR_POLL_RUNNING_MS;
  if (!mirror.eligible) return null;
  return MIRROR_POLL_IDLE_MS;
}

/** What a status read set out with: the read generation (bumped by a
 *  refresh the user asked for, when it is sent and when it is answered) and
 *  the page's status answer it was read after. */
export interface MirrorReadTicket<Seed> {
  gen: number;
  seed: Seed;
}

/** Whether a status read that set out with `ticket` may still be shown now
 *  that the generation is `gen` and the page's own status answer is `seed`.
 *  A read that started before a newer page status, or before a refresh the
 *  user asked for, is older than what the page already shows: dropped. */
export function mirrorReadIsCurrent<Seed>(
  ticket: MirrorReadTicket<Seed>,
  gen: number,
  seed: Seed,
): boolean {
  return ticket.gen === gen && ticket.seed === seed;
}
