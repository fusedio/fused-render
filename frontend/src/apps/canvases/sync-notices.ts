// What the canvas page tells the user when sync changes the canvas on its own
// — the moves nobody clicked for (canvas_snapshot.diff_views, canvases
// `_keep_gone_body`). Pure: CanvasWorkspace raises what this decides, and the
// tests beside this file pin the words.
//
//   A node whose source file was deleted on disk (a Claude session's `rm`,
//   the row left in canvas.toml) is removed from the canvas by the next
//   delta: its delete op says `reason: "file_deleted"`. The notice follows
//   the frame's ACK, not the post, and leaves out the nodes the frame
//   skipped (the user's own newer change kept them).
//
//   Text the user typed into such a node while its file went is never
//   written back as a file (that would undo the deletion), but it is kept in
//   the canvas's trash, and the notice says where.
import type { NotificationInput } from "@platform/lib/notifications";
import type { DeltaOp } from "./api";

export interface Removal {
  name: string;
  /** The source file that went, when the host knew it. */
  file: string | null;
}

/** The deletes in a delta that sync made because a node's file went. */
export function fileDeletedRemovals(ops: readonly DeltaOp[]): Removal[] {
  const out: Removal[] = [];
  for (const op of ops) {
    if (op.op !== "delete" || op.reason !== "file_deleted") continue;
    if (typeof op.name !== "string" || !op.name) continue;
    out.push({ name: op.name, file: typeof op.file === "string" && op.file ? op.file : null });
  }
  return out;
}

const LISTED = 5;

/** "a.py, b.py and 3 more". */
function listed(items: readonly string[]): string {
  if (items.length <= LISTED) {
    return items.length === 1 ? items[0] : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
  }
  return `${items.slice(0, LISTED).join(", ")} and ${items.length - LISTED} more`;
}

/** The notice for nodes removed because their files were deleted; null for none. */
export function removalNotice(removed: readonly Removal[]): NotificationInput | null {
  if (removed.length === 0) return null;
  if (removed.length === 1) {
    const { name, file } = removed[0];
    return {
      title: `Removed "${name}" from the canvas`,
      detail: `Its file${file ? ` ${file}` : ""} was deleted on disk.`,
    };
  }
  return {
    title: `Removed ${removed.length} nodes from the canvas`,
    detail: `Their files were deleted on disk: ${listed(removed.map((r) => r.file ?? r.name))}.`,
  };
}

/** The notice for an edit kept in the trash because its file was deleted.
 *  Retained ("attention"): the path is what the user needs to get the text
 *  back, so it must outlive the popup. */
export function keptEditNotice(file: string, path: string): NotificationInput {
  return {
    title: `Kept your unsaved edits to ${file}`,
    detail: `The file was deleted on disk while you were editing it. Your text was saved to ${path}`,
    tier: "attention",
  };
}
