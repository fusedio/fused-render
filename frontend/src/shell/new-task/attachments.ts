// Attachments on the New task card: the chip model, the picture-or-glyph rule
// and the file-preview URL. Pure; the tiles and the viewer that draw these live
// in AttachmentTiles.tsx / AttachmentViewer.tsx.
import type { ScheduledMessage, StatResult } from "@platform/lib/api";
import { thumbUrl } from "@platform/lib/thumb-frame";

// One attachment, from either of the two places it can come from. A fresh
// attach shows a `blob:` thumbnail (pictures only) while the upload is in
// flight and gains `path` plus the SERVER's `kind` when POST
// /api/schedule/shot answers; an Edit's restored attachment is the opposite — a
// stored path with no blob, its kind read back off the extension
// (`attachmentKindOf`) and its picture drawn through /api/fs/raw. `key` only
// keys the React list.
//
// ANY FILE, NO CAPS (D618, following the chat's D612/D615): the count cap
// (IMAGES_MAX, 4), the byte cap and the image-only MIME gate are all gone. What
// `kind` decides is what the chat's `shotIsImage` decides — a thumbnail or a
// glyph, a picture viewer or a template preview — and nothing about whether the
// file is allowed in.
export interface TaskImage {
  key: number;
  path: string;
  // Thumbnail XOR glyph, never both — the chat's D613 rule and for its reason:
  // a chip wearing a picture frame AND a hole is the worst of the three
  // possible outputs.
  kind: "image" | "file";
  // What to SAY for a file: the client's filename, which is the only name the
  // user recognises — the stored path is a minted timestamp.
  name: string;
  // A `blob:` URL for a freshly attached, drawable picture; null for
  // everything else (a restored attachment, every non-picture, and a picture in
  // a format this engine cannot draw until the server's PNG comes back).
  thumb: string | null;
}

// Extensions that mean "picture" for a path with no File behind it — a restored
// Edit, and the kind guess a fresh attach makes before the upload answers.
// Deliberately only the DRAWABLE formats: the upload endpoint has already
// converted anything a browser shows as an empty box (a `.tif`, a `.heic`) by
// the time a path is stored, so a stored path in one of those formats is the
// original that nobody can draw — and the one worth drawing is the `-view.png`
// beside it, which this list matches on its own.
const DRAWABLE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp",
                               ".avif", ".bmp", ".svg", ".ico"]);
// The same answer from a MIME, for a clipboard paste whose File has no usable
// filename at all (a pasted screenshot is `image/png` and nothing else).
export const DRAWABLE_MIMES = new Set(["image/png", "image/jpeg", "image/gif",
                                "image/webp", "image/avif", "image/bmp",
                                "image/svg+xml"]);

export function attachmentKindOf(path: string): "image" | "file" {
  const dot = path.lastIndexOf(".");
  const slash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const ext = dot > slash ? path.slice(dot).toLowerCase() : "";
  return DRAWABLE_EXTS.has(ext) ? "image" : "file";
}

// An EDIT's chips, restored from the entry the form opened on.
//
// `attachments` first, `images` second, and the difference is what the user
// sees: the richer field carries the filename they chose and the kind the
// browser settled at attach time, where a bare path yields a minted timestamp
// (`20260828-101500-a1b2c3d4.pdf`) and a kind guessed from an extension the
// upload endpoint may have changed. The fallback stays because an entry stored
// before D619 has only paths — and it is the same guess the server makes for
// one (`schedule._derived_attachment`).
export function restoredAttachments(
  editing?: Pick<ScheduledMessage, "images" | "attachments"> | null,
): TaskImage[] {
  const rich = editing?.attachments ?? [];
  if (rich.length) {
    return rich.map((a, i) => ({
      key: i,
      path: a.path,
      kind: a.kind === "image" ? "image" : "file",
      name: a.name || a.path.split("/").pop() || a.path,
      thumb: null,
    }));
  }
  return (editing?.images ?? []).map((p, i) => ({
    key: i,
    path: p,
    kind: attachmentKindOf(p),
    name: p.split("/").pop() || p,
    thumb: null,
  }));
}

// The chat's own two functions, ported (D616 / template.html paneOfferable +
// paneSrcFor): stat's entries minus the `conditional` ones — their verdict lives
// behind /api/fs/conditions and is deliberately NOT fetched, so an unresolved
// gate reads as "not offered" — and minus the chat mode itself; the first one
// wins, and it is the shell's own default-template rule. A per-extension table
// here would drift from the registry on the next rebinding and ignore a user's
// override entirely (§16).
//
// /render and NOT /embed: /embed serves the React shell, which nests the file
// one iframe deeper — an extra document, an extra boot, and a chrome bar around
// a preview that has a caption of its own here.
//
// `thumbUrl` puts the two display-only stamps on: `_preview=1` (this is a
// picture of a page, not an open the recents list should record) and
// `_nofocus=1` (the framed page may not steal the keyboard or yank scroll — the
// viewer is modal and Escape has to keep belonging to it).
//
// null is the ORDINARY answer, not an error: a file with no template, a path
// the pruner has deleted, a server that declined. The dialog then says the name
// and the path, which is what it said before a preview existed.
const PREVIEW_SKIP_MODES = new Set(["claude"]);

export function taskPreviewSrcFor(stat: StatResult | null, path: string): string | null {
  if (!stat || stat.is_dir || !path) return null;
  const t = (stat.templates || []).find(
    (e) => !e.conditional && !PREVIEW_SKIP_MODES.has(e.mode));
  if (!t) return null;
  // `_render` is a shell sentinel (PT-12), not a template folder: it means "the
  // file renders itself", which is a bare /render on the file.
  if (t.mode === "_render") return thumbUrl("/render?path=" + encodeURIComponent(path));
  if (!t.path) return null;
  return thumbUrl("/render?path=" + encodeURIComponent(t.path)
    + "&_file=" + encodeURIComponent(path)
    + (stat.remote ? "&_remote=1" : ""));
}
