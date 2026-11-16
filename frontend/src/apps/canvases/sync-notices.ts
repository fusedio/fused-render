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

// The card draws the title and the detail on ONE line each, ellipsized
// (notifications.css `.dl-title`), so the words that matter come first and
// every line stays short: the node's name leads the title, and the path of a
// kept edit is behind an action rather than in the text.

/** The notice for nodes removed because their files were deleted; null for none. */
export function removalNotice(removed: readonly Removal[]): NotificationInput | null {
  if (removed.length === 0) return null;
  if (removed.length === 1) {
    return { title: `Removed ${removed[0].name}`, detail: "Its file was deleted on disk." };
  }
  return {
    title: `Removed ${removed.length} nodes from the canvas`,
    detail: "Their files were deleted on disk.",
  };
}

/** The notice for an edit kept in the trash because its file was deleted.
 *  Retained ("attention"): the kept text is what the user needs, so it must
 *  outlive the popup; `open` shows the kept file. */
export function keptEditNotice(file: string, open: () => void): NotificationInput {
  return {
    title: "Kept your unsaved edits",
    detail: `${file} was deleted on disk.`,
    tier: "attention",
    action: { label: "Open", onClick: open, title: "Open the kept text" },
  };
}
