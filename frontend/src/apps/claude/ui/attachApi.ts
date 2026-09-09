// THE ATTACHMENT SEAM: what the chip row, the viewer, the receipts and the
// "what was sent" popup need from the pipeline, and the few decisions that are
// the UI's own.
//
// The VOCABULARY IS NOT REDEFINED HERE. `shotNoun` / `shotAlt` / `sizeLabel` /
// `failLabel` / `receiptFor` / `receiptsFromWire` / `prunedLabel` come from
// `shots/attach.ts` and are re-exported, for the reason T keeps one copy of each
// (T:7067): "a pasted image called 'preview screenshot' in the receipt is a
// receipt that describes the wrong thing". What lives here is the drawing side —
// picture-or-glyph, door-or-not, what the viewer will open — plus `AttachApi`,
// the injectable face of the pipeline the tray calls.
//
// Sources: T:7067-7215, T:10637-10877, inventory 03 §A/§B/§E.
import { statPath } from "@platform/lib/api";

import { paneOfferable, paneSrcFor } from "../pane/paneUrl";
import {
  attachFiles,
  attachPane,
  attachPaths,
  dragHasAttachment,
  filesFromPaste,
  flash,
  pathsFromDrop,
  probePruned,
  readDirs,
  readDirsFor,
  receiptFor,
  revoke,
  sizeLabel,
  toWire,
} from "../shots";
import type { Attachment, Receipt, ShotKind } from "../shots/types";

export {
  failLabel,
  prunedLabel,
  receiptFor,
  receiptsFromWire,
  settleReceipts,
  shotAlt,
  shotNoun,
  sizeLabel,
} from "../shots";

/**
 * The pipeline as the tray calls it. Every member is `shots/*.ts`'s own; there
 * is no implementation here.
 *
 * INJECTABLE for the reason `ControllerDeps.run` is: a chip row's ordering, its
 * one pane seat and its hand-back on a failed send are all decisions this half
 * of the app makes, and testing them must not need a capture engine, a
 * clipboard or a server.
 */
export interface AttachApi {
  flash: typeof flash;
  attachPane: typeof attachPane;
  attachFiles: typeof attachFiles;
  attachPaths: typeof attachPaths;
  readDirs: typeof readDirs;
  /** `readDirs` against the shots dir the pipeline has already resolved for
   *  this agent — the synchronous read T's `shotDirSeen` exists for (T:9018). */
  readDirsFor: typeof readDirsFor;
  revoke: typeof revoke;
  toWire: typeof toWire;
  receiptFor: typeof receiptFor;
  probePruned: typeof probePruned;
  dragHasAttachment: typeof dragHasAttachment;
  pathsFromDrop: typeof pathsFromDrop;
  filesFromPaste: typeof filesFromPaste;
}

/** The real pipeline. One object rather than twelve imports at every call site,
 *  and the seam a test replaces wholesale. */
export const ATTACH_API: AttachApi = {
  flash,
  attachPane,
  attachFiles,
  attachPaths,
  readDirs,
  readDirsFor,
  revoke,
  toWire,
  receiptFor,
  probePruned,
  dragHasAttachment,
  pathsFromDrop,
  filesFromPaste,
};

// ---- what gets drawn (T:7129-7215, T:10717-10760) --------------------------

/** Anything the chip row, the viewer, a receipt or the popup can show: the union
 *  of `Attachment` and `Receipt` as far as the DRAWING is concerned. `src` is
 *  what to display (a blob URL this session, `/api/fs/raw` for a restored turn)
 *  and `view` is the path the agent was handed, which is a different string and
 *  the one worth showing (T:7195-7203). */
export interface Viewable {
  /** The `Attachment.id` this was drawn from, where there was one. Discard looks
   *  the attachment up BY THIS: every refusal has `view: null`, so matching on
   *  kind+view discarded the wrong chip of two failed pictures (PR2 review). */
  id?: string;
  kind: ShotKind;
  view: string | null;
  src?: string;
  thumb?: string;
  size?: number;
  name?: string;
  viewNote?: string;
  why?: string;
  /** Still in the tray: only then can Discard tell the truth (T:10768). */
  pending?: boolean;
}

/** T:7143-7148 / T:10832-10834 — A PICTURE OR A GLYPH, NEVER BOTH, and the
 *  glyph is a DOOR only where there is a room behind it: a real path, and one of
 *  the two kinds the viewer opens without pixels. A refused attachment keeps the
 *  plain span, so one "failed" chip shape serves whatever kind failed. */
export function glyphDoor(shot: Viewable): boolean {
  return !shot.src && !shot.thumb && !!shot.view && (shot.kind === "file" || shot.kind === "image");
}

/** T:7141's glyph is `ui/AttachIcon` now — a lucide icon rather than 📄/🖼
 *  (P2-7), so it cannot be a string and does not belong in this module. */

/** T:10722-10726 `shotViewOpen`'s admission test: something to show, or a file /
 *  undecodable image — a real path, a real size and no pixels, which is a file
 *  in every way that matters here. */
export function viewerOpens(shot: Viewable | null | undefined): boolean {
  if (!shot) return false;
  if (shot.src || shot.thumb) return true;
  return !!shot.view && (shot.kind === "file" || shot.kind === "image");
}

/** A PENDING attachment, as everything that draws one sees it. */
export function toViewable(att: Attachment, pending = true): Viewable {
  return {
    id: att.id,
    kind: att.kind,
    view: att.view,
    ...(att.thumb ? { thumb: att.thumb } : {}),
    ...(typeof att.size === "number" ? { size: att.size } : {}),
    ...(att.name ? { name: att.name } : {}),
    ...(att.viewNote ? { viewNote: att.viewNote } : {}),
    ...(att.why ? { why: att.why } : {}),
    pending,
  };
}

/** A SENT receipt, as everything that draws one sees it. */
export function receiptViewable(r: Receipt): Viewable {
  return {
    ...(r.id ? { id: r.id } : {}),
    kind: r.kind,
    view: r.view,
    ...(r.src || r.thumb ? { src: r.src || r.thumb } : {}),
    ...(typeof r.size === "number" ? { size: r.size } : {}),
    ...(r.name ? { name: r.name } : {}),
    ...(r.viewNote ? { viewNote: r.viewNote } : {}),
    ...(r.why ? { why: r.why } : {}),
    pending: false,
  };
}

/** T:10891 — an `<img>` that 404s. Said in words rather than left as a broken
 *  image, which a reader takes for a bug in the chat rather than for a temp file
 *  that expired. */
export const SHOT_GONE = "screenshot no longer on disk";

/** T:7099 — a size is only ever shown for something with no picture to look at:
 *  a picture answers "is this the right one" by being looked at, where a
 *  spreadsheet or a log answers it by name and size and nothing else. */
export function shownSize(shot: Viewable): string {
  return shot.src || shot.thumb ? "" : sizeLabel(shot.size);
}

// ---- an attachment's own preview (T:10712 `shotPreviewSrc`, D616) ---------

/**
 * The file's own fused-render template, framed inside the viewer. EXACTLY the
 * pane's rule through the pane's own two functions — stat's offerable entries
 * minus the conditional ones, first one wins, then `paneSrcFor` for the URL.
 * Reused rather than re-derived, because a per-extension table here would drift
 * from the registry on the next rebinding and ignore a user's own override
 * (§16).
 *
 * `null` is the ORDINARY answer, not an error: a file with no template, a path
 * the pruner has deleted, a server that declined. The viewer then stays exactly
 * as it was before D616 — name, size, path, Discard, Close.
 */
export async function previewSrcFor(path: string | null): Promise<string | null> {
  if (!path) return null;
  try {
    // T reads `{error}` off the body; `statPath` THROWS on a non-ok reply
    // (platform/lib/api), and the catch below is the same answer.
    const st = await statPath(path);
    if (!st || st.is_dir) return null;
    const entry = paneOfferable(st.templates)[0];
    if (!entry) return null;
    // The two stamps the shell puts on a display-only frame: this is a picture
    // of a page, not an open the recents list should record, and the framed page
    // may not steal the keyboard — the viewer is modal, and Escape has to keep
    // belonging to it.
    return paneSrcFor(entry, path, !!st.remote, { preview: true, noFocus: true });
  } catch {
    return null;
  }
}
