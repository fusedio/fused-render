// FROM A GESTURE TO AN ATTACHMENT (T:11258-11750, 7067-7215, 10645, 10855,
// 10903, 16519).
//
// Four gestures, one pipeline: the camera (`attachPane`), a paste or a drop of
// BYTES (`attachFiles`), a drag from inside fused-render that carried a real
// PATH (`attachPaths`), and the send-time badged overview (`attachOverview`).
// All four land in the shots directory, wear a chip, open the same viewer, ride
// the same `<pane-shot>` block and leave the same receipt — which is the whole
// design: that directory is already the one path `--allowed-tools` pre-approves a
// `Read` of, already pruned (12h/200 files), already served back through
// `/api/fs/raw` for a restored turn.
//
// Nothing here throws for a reason the user could act on. Every refusal becomes
// an attachment carrying its own `why`, so nothing the user gestured at
// disappears without an answer — and since D615 exactly ONE thing still refuses:
// an upload that failed.
import { rawUrl, uploadFile } from "@platform/lib/api";
import { runAgent } from "../protocol/agent";
import type { PaneShotWire } from "../protocol/wire";
import { capturePane, captureOverview, type CaptureOptions } from "./capture";
import { viewNoteFrom } from "./dom-capture";
import { shotExt, shotPixels, shrink } from "./encode";
import {
  SHOT_MIME_EXT,
  SHOT_SUFFIX_OVERVIEW,
  SHOT_SUFFIX_VIEW,
  shotBase,
  shotDirOf,
  shotFileExt,
  shotJoin,
  shotStamp,
  shotsDir,
  shotsDirSeen,
} from "./dir";
import {
  SHOT_ATTACH_MAX_BYTES,
  SHOT_PATH_TYPE,
  type Attachment,
  type PaneShotEntry,
  type Receipt,
  type ShotBadge,
  type ShotKind,
} from "./types";

// ── what it IS (T:11366-11430) ──────────────────────────────────────────────

/** Anything with the two fields the kind rules read. A `File` is one; so is the
 *  `{name, type:""}` a real-path drag synthesises (T:11683). */
export interface NamedBlob {
  name?: string;
  type?: string;
}

/** Whether this file is a picture at all. `type` first because it is what the
 *  clipboard supplies and what the browser sniffed; the extension is the fallback
 *  for a drag whose source gave no type. It no longer decides whether the
 *  attachment HAPPENS — every file type is accepted now (T:11392). */
export function isImage(file: NamedBlob | null | undefined): boolean {
  if (!file) return false;
  if ((file.type || "").slice(0, 6) === "image/") return true;
  return /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(file.name || "");
}

/** WHAT the attachment will be called on the wire (T:11366). */
export function kindFor(file: NamedBlob): ShotKind {
  return isImage(file) ? "image" : "file";
}

/** What to call the COPY on disk. A picture's name follows its BYTES, because a
 *  clipboard image arrives with no filename and an agent Reading a `.png` that
 *  holds JPEG bytes gets a file it cannot open. A file keeps its own extension
 *  verbatim, and keeps NONE when it has none — the `.png` fallback that is right
 *  for a nameless blob would be a lie about a Makefile (T:11403). */
export function saveExt(file: NamedBlob): string {
  if (isImage(file)) return shotFileExt(file.type, file.name);
  const n = file.name || "";
  const dot = n.lastIndexOf(".");
  return dot > 0 ? n.slice(dot).toLowerCase() : "";
}

/** The FORMAT a name or path claims, spelled the way a person says it. Only ever
 *  used in the sentence a converted attachment wears, and that sentence has to
 *  name the format the user recognises: they dropped `IMG_4031.HEIC`, so
 *  "converted from HEIC" is checkable against the chip next to it (T:11416). */
export function formatLabel(name: string | null | undefined): string {
  const s = String(name || "");
  const dot = s.lastIndexOf(".");
  const ext = dot > 0 ? s.slice(dot + 1).toLowerCase() : "";
  const long: Record<string, string> = {
    jpg: "JPEG",
    jpeg: "JPEG",
    tif: "TIFF",
    tiff: "TIFF",
    heic: "HEIC",
    heif: "HEIF",
  };
  return long[ext] || (ext ? ext.toUpperCase() : "that format");
}

// ── the words (T:7067-7120) ─────────────────────────────────────────────────

/** What one attachment IS, in the words the chip, the viewer and the alt text all
 *  use. ONE place, because a pasted image called "preview screenshot" in the
 *  receipt is a receipt that describes the wrong thing (T:7067). */
export function shotNoun(att: Pick<Attachment, "kind" | "name"> | null, paneNoun = "preview"): string {
  if (!att) return "picture";
  if (att.kind === "image") return att.name || "pasted image";
  if (att.kind === "file") return att.name || "attached file";
  if (att.kind === "overview") return "annotated overview";
  return paneNoun + " screenshot";
}

/** The alt text / aria label (T:7075). */
export function shotAlt(att: Pick<Attachment, "kind" | "name"> | null, paneNoun = "preview"): string {
  if (att && att.kind === "image") {
    return "image attached to this message" + (att.name ? ": " + att.name : "");
  }
  if (att && att.kind === "file") {
    return "file attached to this message" + (att.name ? ": " + att.name : "");
  }
  if (att && att.kind === "overview") {
    return "overview of the " + paneNoun + " pane with a badge at each comment";
  }
  return "screenshot of the whole " + paneNoun + " pane";
}

/** How big it is, in the one unit a reader can act on — only ever shown for a
 *  FILE. `undefined` is a real and ordinary answer: an attachment that came in as
 *  a PATH has no size to report, and inventing "0 KB" for it would read as an
 *  empty file (T:7092). */
export function sizeLabel(bytes: number | undefined): string {
  if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

/** What a REFUSED attachment says, on the row, in both places one is drawn (the
 *  pending chip and the sent receipt). ONE function because the two used to spell
 *  it differently ("image failed" against "no image"), which made a receipt look
 *  like a different event from the chip it replaced. `why` is ONE SHORT CLAUSE so
 *  it fits on the row; the full sentence stays in the title and the viewer's note
 *  line (T:7112). */
export function failLabel(att: Pick<Attachment, "kind" | "why">): string {
  const what =
    att.kind === "image"
      ? "no image"
      : att.kind === "file"
        ? "no file"
        : att.kind === "overview"
          ? "no overview screenshot"
          : "no pane screenshot";
  return att.why ? what + " — " + att.why : what;
}

// ── the camera and the overview (T:10088, 10127) ────────────────────────────

function newId(): string {
  return crypto.randomUUID();
}

/** One picture of the whole visible pane, uploaded, as an Attachment — or one
 *  with `view: null` naming what stopped it. The pane SEAT is unique (a second
 *  click replaces the first, T:11309): that is the tray's swap to make, so this
 *  only marks the seat (T:10088, 11258). */
export async function attachPane(
  agentDir: string,
  frame: HTMLIFrameElement | null,
  opts: CaptureOptions = {},
): Promise<Attachment> {
  const dir = beginShotsDir(agentDir);
  const shot = await capturePane(frame, opts);
  return uploadCapture(agentDir, shot, "pane", SHOT_SUFFIX_VIEW, dir);
}

/** T:10083 asks `shotDirPath()` BEFORE `shotPane()`, so an unresolvable shots
 *  dir costs nothing. Here it is started ALONGSIDE the capture instead: the
 *  round trip overlaps the style walk in the ordinary case, and the dead-dir
 *  case still ends in the chip T's own upload failure ends in ("it could not be
 *  saved"), rather than trading that sentence for a serialized request on every
 *  capture. Rejections are parked here so the in-flight promise cannot become an
 *  unhandled rejection while the capture is still running; `uploadCapture`
 *  awaits it inside its own try. */
function beginShotsDir(agentDir: string): Promise<string> {
  const p = shotsDir(agentDir);
  p.catch(() => {});
  return p;
}

/**
 * The send-time overview: ONE whole-pane screenshot with a red letter badge
 * burned in at each note's spot (T:10127).
 *
 * EXPORTED BUT UNWIRED, AND DELIBERATELY PR3's. Nothing in PR2 has notes to
 * badge, so there is no caller — and the seam it still needs belongs with the
 * caller rather than here: T's failed-send path revokes the OVERVIEW while
 * keeping the user's own pictures (T:16698-16708), because the overview is the
 * page's own picture of a pane that has since moved on, where a picture the user
 * attached may not be retakeable. `ClaudeChat`'s `onSendReturned` hands the
 * whole tray back today, which is right for a tray that only ever holds the
 * user's; the asymmetry arrives with the first overview and is PR3's to land
 * with it.
 */
export async function attachOverview(
  agentDir: string,
  frame: HTMLIFrameElement | null,
  badges: ShotBadge[],
  opts: CaptureOptions = {},
): Promise<Attachment> {
  const dir = beginShotsDir(agentDir);
  const shot = await captureOverview(frame, badges, opts);
  return uploadCapture(agentDir, shot, "overview", SHOT_SUFFIX_OVERVIEW, dir);
}

async function uploadCapture(
  agentDir: string,
  shot: Awaited<ReturnType<typeof capturePane>>,
  kind: ShotKind,
  suffix: string,
  dirp?: Promise<string>,
): Promise<Attachment> {
  const att: Attachment = { id: newId(), kind, view: null };
  if (kind === "pane") att.seat = "pane";
  if (!shot.blob) {
    // A failed capture still becomes a chip, and still rides the message:
    // degrading to "no image" is right, degrading to "no evidence anything was
    // asked for" is the silent-failure shape (T:11305).
    att.viewNote = shot.why;
    return att;
  }
  try {
    const dir = await (dirp ?? shotsDir(agentDir));
    const path = shotJoin(dir, shotStamp() + suffix + shotExt(shot.blob));
    await uploadFile(path, shot.blob, shotBase(path));
    att.view = path;
    if (shot.thumb) att.thumb = shot.thumb;
    // Never suppressed for a blank canvas, only annotated: the caveat rides
    // beside the real picture (T:10104).
    const caveats = viewNoteFrom(shot.notes, shot.incomplete ?? false);
    if (caveats) att.viewNote = caveats;
    return att;
  } catch (err) {
    // The bytes never landed, so the thumbnail is a handle to a Blob nothing will
    // ever show — released here, since no chip is going to hold it.
    att.thumb = shot.thumb;
    revoke(att);
    att.thumb = undefined;
    att.view = null;
    att.why = "could not be saved";
    att.viewNote = "not attached: it could not be saved (" + message(err) + ")";
    return att;
  }
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ── pasted and dropped BYTES (T:11530-11628) ────────────────────────────────

/** `shotPixels`, which is a decode and can therefore reject (a codec that gives
 *  up mid-frame, a browser that refuses the bytes). An undecodable picture is
 *  not a refusal — it is the road that asks the server for a PNG — so a
 *  rejection here answers exactly as `null` does. */
async function pixelsOf(file: File): Promise<Awaited<ReturnType<typeof shotPixels>>> {
  try {
    return await shotPixels(file);
  } catch {
    return null;
  }
}

/** Take one thing the user brought in. Never throws.
 *
 *  AN OVERSIZE PICTURE IS RESIZED, NOT REFUSED (D615): the 4 MiB number was
 *  never about the disk, it is about the agent's read of the pixels, and a
 *  picture too big for it is one downscale away from being exactly as useful.
 *  There is no cap at all behind it for a file or an undecodable image (D612). */
export async function attachFile(agentDir: string, file: File): Promise<Attachment> {
  const kind = kindFor(file);
  const name = file.name || (kind === "image" ? "pasted image" : "attached file");
  // What actually gets written, what the chip says happened to it, and whether a
  // 22px view of it would show anything. Decided in this order because each
  // answer feeds the next (T:11549).
  let blob: Blob = file;
  let note = "";
  let pic = kind === "image";
  let undecodable = false;
  if (kind === "image") {
    const pix = await pixelsOf(file);
    if (!pix) {
      // Still attached, and still called an image — but the note is NOT written
      // here, because what it should say is not known yet: the server gets asked
      // for a PNG after the upload (T:11554).
      pic = false;
      undecodable = true;
    } else {
      // `pix` holds an object URL the <img> road minted, and `shrink`/`encode`
      // can THROW — `drawImage` on a tainted or zero-dimension canvas does — so
      // the release is a finally and not the branch's last statement. A leak
      // here pins the full-size decode for the life of the page.
      try {
        if (file.size > SHOT_ATTACH_MAX_BYTES) {
          const small = await shrink(pix, SHOT_ATTACH_MAX_BYTES);
          if (small) {
            blob = small.blob;
            note =
              (small.width < pix.width
                ? "downscaled from " +
                  pix.width +
                  "×" +
                  pix.height +
                  " to " +
                  small.width +
                  "×" +
                  small.height
                : "re-encoded") +
              " for the agent's read (" +
              sizeLabel(file.size) +
              " → " +
              sizeLabel(blob.size) +
              ")";
          }
        }
      } catch {
        // THE DOWNSCALE IS A TRIGGER, NOT A GATE (T:11518): the 4 MiB number is
        // about the agent's read of the pixels, never about whether the picture
        // may be attached at all. `drawImage` on a tainted or zero-dimension
        // canvas throws, and that throw used to escape this function entirely —
        // `addFiles` then removed the placeholder without a chip in its place,
        // so a dropped picture vanished with no answer (Bugbot, PR #1064).
        //
        // So the original bytes go up instead, unresized and unannotated: a
        // picture the agent has to downscale its own read of is worth
        // immeasurably more than no picture.
        blob = file;
        note = "";
      } finally {
        pix.free();
      }
    }
  }
  try {
    const dir = await shotsDir(agentDir);
    // The extension follows whichever bytes are going up: the file's own when the
    // file itself is being copied, and the ENCODER'S when it was re-encoded — a
    // path called `.jpg` holding webp bytes is the exact lie `shotExt` prevents
    // (T:11567).
    const ext = blob === file ? saveExt(file) : shotExt(blob);
    const path = shotJoin(dir, shotStamp() + ext);
    await uploadFile(path, blob, name);
    // THE ONE THING THIS BROWSER CANNOT DO FOR ITSELF: the bytes are on disk now,
    // inside the one directory the server will convert in, so a picture no engine
    // here can decode gets one more chance from the side that has Pillow and (on
    // a Mac) the OS's own decoders (T:11574).
    let conv: { path: string; width: number; height: number; source_w: number; source_h: number } | null =
      null;
    if (undecodable) {
      conv = await serverPng(agentDir, path);
      note = conv
        ? "converted from " +
          formatLabel(name) +
          " to " +
          formatLabel(conv.path) +
          " on the server (" +
          conv.source_w +
          "×" +
          conv.source_h +
          " → " +
          conv.width +
          "×" +
          conv.height +
          ") because neither the browser nor the agent can read that format"
        : "attached as bytes: this browser cannot decode " +
          name +
          " and the " +
          "server could not convert it either, so the agent probably cannot " +
          "read this format — say so rather than guessing at the pixels";
    }
    // A thumbnail and a `size` are ALTERNATIVES, not a pair: whatever can be SEEN
    // answers "which one did I just attach" by being looked at, and whatever
    // cannot answers it by name and size. A CONVERTED picture is on the seen side
    // of it, and carries no `size` for exactly the reason no other drawable
    // picture does — which is what keeps the restore path's `bare` test true
    // (T:11597).
    const att: Attachment = { id: newId(), kind, view: conv ? conv.path : path, name };
    if (note) att.viewNote = note;
    if (conv) att.thumb = rawUrl(conv.path);
    else if (pic) att.thumb = URL.createObjectURL(blob);
    else att.size = file.size;
    return att;
  } catch (err) {
    return {
      id: newId(),
      kind,
      view: null,
      name,
      viewNote: "not attached: it could not be saved (" + message(err) + ")",
      why: "could not be saved",
    };
  }
}

/** EVERYTHING in a paste or a drop, in order. Sequential rather than
 *  `Promise.all`, and that is the one decision this function still makes: they
 *  share one shots directory and one upload channel, and the chips appearing one
 *  after another as they land IS the feedback for a drop big enough to take a
 *  moment. A parallel fan-out would buy little and would land the chips in a
 *  scrambled order (T:11618). No count cap (D617). */
export async function* attachFiles(agentDir: string, files: File[]): AsyncIterable<Attachment> {
  for (const f of (files || []).filter(Boolean)) {
    yield await attachFile(agentDir, f);
  }
}

/** `image_to_png`: never throws and never rejects — a failure here leaves the
 *  attachment exactly as D613 left it (bytes, a glyph, a note), so the only thing
 *  riding on this call is how GOOD the attachment is, never whether there is one
 *  (T:11438). */
async function serverPng(
  agentDir: string,
  path: string,
): Promise<{ path: string; width: number; height: number; source_w: number; source_h: number } | null> {
  try {
    const out = await runAgent(agentDir, "image_to_png", { path }, { key: null });
    return out && "path" in out && out.path ? out : null;
  } catch {
    return null;
  }
}

// ── an attachment that is already a PATH (T:11634-11696) ────────────────────

/** No upload, no bytes read, no size (the page never opened the file —
 *  `undefined` is the honest answer). An image still gets a thumbnail, because
 *  `/api/fs/raw` will serve it and a picture the user can see is the whole reason
 *  the thumbnail exists. Copying would be the WRONG answer even though it would
 *  work: the file is one the user already has, at a path they can name, and the
 *  agent is about to be asked to Read it — likely to EDIT it next, which a copy
 *  in a directory pruned in 12 hours cannot survive (T:11680). */
export function attachPaths(_agentDir: string, paths: string[]): Attachment[] {
  const out: Attachment[] = [];
  for (const path of paths || []) {
    const name = shotBase(path);
    const kind = kindFor({ name, type: "" });
    const att: Attachment = { id: newId(), kind, view: path, name, brought: true };
    if (kind === "image") att.thumb = rawUrl(path);
    out.push(att);
  }
  return out;
}

/** Every directory the agent has to be allowed to Read for THIS message, beyond
 *  the shots dir it is always allowed. Only real-path attachments contribute:
 *  everything that was copied lives under the shots dir, which the spawn line
 *  pre-approves unconditionally, so filtering it out is what keeps the
 *  `--allowed-tools` line from growing a duplicate rule on every turn.
 *  Deduplicated, because dropping six rows out of one folder is one grant
 *  (T:11698). */
export function readDirs(
  attachments: Pick<Attachment, "view">[] | null | undefined,
  dir?: string,
): string[] {
  const skip = dir || "";
  const dirs: string[] = [];
  for (const s of attachments || []) {
    if (!s || !s.view) continue;
    const d = shotDirOf(s.view);
    if (!d || d === skip) continue;
    if (dirs.indexOf(d) === -1) dirs.push(d);
  }
  return dirs;
}

/** `readDirs` against whatever `shotsDir` has already answered for `agentDir` —
 *  the synchronous read T's `shotDirSeen` exists for (T:9018). */
export function readDirsFor(
  agentDir: string,
  attachments: Pick<Attachment, "view">[] | null | undefined,
): string[] {
  return readDirs(attachments, shotsDirSeen(agentDir));
}

// ── cleanup (T:10645) ───────────────────────────────────────────────────────

/** Release an attachment's thumbnail and forget it. The blob URL is the only
 *  handle to a Blob the size of a full-pane PNG, so an unrevoked one pins it for
 *  the life of the page. Idempotent — the field is cleared — because both a drop
 *  and an abandoned capture can reach it. A `rawUrl` thumb is not an object URL
 *  and revoking it is a no-op, which is why the same call is safe for every
 *  kind. */
export function revoke(att: Attachment | null | undefined): void {
  if (!att || !att.thumb) return;
  if (att.thumb.startsWith("blob:")) {
    try {
      URL.revokeObjectURL(att.thumb);
    } catch {
      /* already gone */
    }
  }
  att.thumb = "";
}

// ── the wire (T:16519) ──────────────────────────────────────────────────────

/** Each picture goes on the wire as its path, its note, its kind, its name and
 *  nothing else: `thumb` is a blob URL belonging to THIS page, a dead link
 *  everywhere else and unreadable to the agent (T:16519).
 *
 *  The overview rides the same block, FIRST — it is the picture the annotations
 *  block tells the model to read (T:16545). */
export function toWire(attachments: Attachment[] | null | undefined): PaneShotEntry[] {
  const out: PaneShotEntry[] = [];
  for (const s of attachments || []) {
    if (!s) continue;
    const wire: PaneShotEntry = { kind: s.kind || "pane", view: s.view || null };
    if (s.viewNote) wire.viewNote = s.viewNote;
    // T:16546-16550 builds the overview entry out of kind/view/viewNote and
    // NOTHING else: it is the page's own picture of the pane, so it has no name
    // the user gave it, no size worth quoting, and its refusal is already the
    // whole of `viewNote`. `why` is the receipt's short clause for an attachment
    // the USER brought (see below), and an overview has no receipt of its own.
    if (s.kind === "overview") {
      out.unshift(wire);
      continue;
    }
    // The short clause too, and for the SCREEN rather than for the model: a
    // restored turn rebuilds its receipt out of this JSON alone, and without it a
    // refusal that said why while the page was open goes back to saying "no
    // image" and nothing else the moment the session is reopened.
    if (s.why) wire.why = s.why;
    // `name` rides for both kinds the user BROUGHT IN, and the block's own text
    // leans on it — a capture of this pane has no such name (T:16532).
    if (s.name && (s.kind === "image" || s.kind === "file")) wire.name = s.name;
    // And how big, for a file only — plus an image the browser could not decode,
    // for the same reason: there is no small picture of it to look at, so this is
    // the only measurement of it there is. Absent when unknown (a real-path
    // attachment was never opened) rather than sent as 0 (T:16539).
    if ((s.kind === "file" || s.kind === "image") && typeof s.size === "number") {
      wire.size = s.size;
    }
    out.push(wire);
  }
  return out;
}

// ── receipts (T:10815-10930) ────────────────────────────────────────────────

/** The row under a sent user turn. ONE builder for the live send and for a
 *  restored transcript: a screenshot visible while the session lasts and gone the
 *  moment it is reopened is a screenshot the user cannot rely on having sent
 *  (T:10815). */
export function receiptFor(att: Attachment): Receipt {
  const label = att.view
    ? att.kind === "image"
      ? "image attached" + (att.name ? ": " + att.name : "")
      : att.kind === "file"
        ? "file attached" + (att.name ? ": " + att.name : "")
        : att.kind === "overview"
          ? "annotated overview attached"
          : "screenshot attached"
    : failLabel(att);
  const r: Receipt = { id: att.id, kind: att.kind, label, view: att.view };
  // Never `src` for a file: an <img> pointed at a .csv is a broken-image glyph,
  // which reads as a bug in the chat (T:10820).
  if (att.kind !== "file" && att.thumb) {
    r.thumb = att.thumb;
    r.src = att.thumb;
  }
  if (att.viewNote) r.viewNote = att.viewNote;
  if (att.name) r.name = att.name;
  if (typeof att.size === "number") r.size = att.size;
  if (att.why) r.why = att.why;
  return r;
}

/** The receipts for a turn read back out of a session file. `src` is added here
 *  because only the live page knows how to fetch a path.
 *
 *  A picture gets a `src` (it is drawn); a FILE gets a `probe` instead (there is
 *  nothing to draw, and the only question left about it is whether the path it
 *  names is still there). An IMAGE carrying a `size` is one the sending browser
 *  could not decode — `attachFile` writes a thumbnail OR a size, never both — so
 *  it has no pixels this browser can draw either, and it takes the file's
 *  treatment rather than a broken `<img>` that would then blame the pruner for a
 *  format (T:10903). */
export function receiptsFromWire(entries: PaneShotWire[] | null | undefined): Receipt[] {
  const out: Receipt[] = [];
  for (const shot of entries || []) {
    const kind = (shot.kind || "pane") as ShotKind;
    const att: Attachment = {
      id: newId(),
      kind,
      view: shot.view || null,
      name: shot.name,
      size: shot.size,
      viewNote: shot.viewNote,
      why: shot.why,
    };
    const r = receiptFor(att);
    const bare = kind === "file" || (kind === "image" && typeof shot.size === "number");
    if (shot.view) {
      if (bare) r.probe = rawUrl(shot.view);
      else {
        r.src = rawUrl(shot.view);
        r.thumb = r.src;
      }
    }
    out.push(r);
  }
  return out;
}

/** A restored turn whose file the shots directory has pruned (12h/200 files).
 *  Said in words rather than left as a broken-image glyph, which a reader takes
 *  for a bug in the chat rather than for a temp file that expired (T:10875). */
export async function probePruned(receipt: Receipt): Promise<boolean> {
  if (!receipt.probe) return false;
  try {
    const res = await fetch(receipt.probe, { method: "HEAD" });
    return !res.ok;
  } catch {
    // A DELIBERATE DIVERGENCE FROM T, and the one in this module. T:10869-10879
    // is `.then(r => { if (!r.ok) throw }).catch(append)`, so one `catch` serves
    // both a 404 and a rejected fetch — and a request that never left the
    // machine therefore labels a file that is still on disk "— file pruned".
    // A blip is not a verdict: only the SERVER can say the pruner has been
    // through, so an unanswered question leaves the row saying what it said.
    return false;
  }
}

/** The words a pruned glyph row appends (T:10883). */
export function prunedLabel(kind: ShotKind): string {
  return " — " + (kind === "image" ? "image" : "file") + " pruned";
}

// ── the gestures' own reading of a DataTransfer (T:11644-11750) ─────────────

/** TWO payloads answer yes, and the order they are TRIED in is the whole point:
 *  a drag from inside fused-render carries the real path, and a copy of a file
 *  the user already has is strictly worse than the file itself (T:11742). */
export function dragHasAttachment(dt: DataTransfer | null | undefined): boolean {
  if (!dt) return false;
  const types = Array.from(dt.types || []);
  return types.indexOf("Files") !== -1 || types.indexOf(SHOT_PATH_TYPE) !== -1;
}

/** Real paths a drop carried, one per line. Newline-joined because a multi-row
 *  selection is one drag, and a newline is the one character a filesystem path
 *  cannot contain on the platforms this ships to (T:11665). */
export function pathsFromDrop(dt: DataTransfer | null | undefined): string[] {
  if (!dt || Array.from(dt.types || []).indexOf(SHOT_PATH_TYPE) === -1) return [];
  let raw = "";
  try {
    raw = dt.getData(SHOT_PATH_TYPE) || "";
  } catch {
    return [];
  }
  return raw
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** PASTE. `clipboardData.files` is empty for a clipboard that holds only text, so
 *  a paste of words falls through untouched to the textarea — which is the
 *  important half: this listener sits on a box a user types in all day, and
 *  stealing an ordinary paste would be a far worse bug than never having had the
 *  feature. The caller `preventDefault`s ONLY when this answers non-empty
 *  (T:11719). */
export function filesFromPaste(e: { clipboardData?: DataTransfer | null }): File[] {
  const data = e.clipboardData;
  if (!data) return [];
  return Array.from(data.files || []);
}

/** Files a drop carried, when it carried no real path (T:11775). */
export function filesFromDrop(dt: DataTransfer | null | undefined): File[] {
  if (!dt) return [];
  return Array.from(dt.files || []);
}

export { SHOT_MIME_EXT };
