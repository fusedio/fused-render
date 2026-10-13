// Getting files INTO the embed stage's Files mode — the three doors and the
// two rules they share.
//
// The doors: the OS dialog raised in the server process (`pickFiles`, which
// hands back real paths), a drag out of the app's own Explorer (which carries
// a path in `application/x-fused-path` or a `file://` URI), and a drag out of
// Finder/the desktop (which carries BYTES and strips the path on purpose, so
// the bytes are written into the app's cache first and the stage embeds the
// copy — exactly how the transcribe stage lands a dropped recording).
//
// The rules: what is accepted is decided by EXTENSION, because that is what
// the server decides by (`embed_common.media_kind`) and a file the route would
// refuse by name must not get into the list; and the list is deduplicated and
// capped here, in one place, so the dialog and both drops cannot disagree.
import { getConfig, mkdir, uploadFile } from "@platform/lib/api";

export type MediaKind = "picture" | "audio" | "video" | "text" | "other";

/** The same lists as `embed_common.AUDIO_SUFFIXES` / `VIDEO_SUFFIXES` and
 *  `open_image`'s readable set on the server — so the picker never offers,
 *  and a drop never admits, a file the route then refuses. */
export const PICTURE_TYPES = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif"];
export const AUDIO_TYPES = ["wav", "mp3", "flac", "ogg", "oga", "m4a", "aac", "opus"];
export const VIDEO_TYPES = ["mp4", "m4v", "mov", "mkv", "webm", "avi", "mpg", "mpeg"];
/** Text files are not embedded by PATH at all — they are LINES, and belong to
 *  the Lines mode's corpus. Named so a drop can route them there. */
export const TEXT_TYPES = ["txt", "md", "markdown", "csv"];

/** The in-app drag payload: a path, or several newline-separated. */
export const FUSED_PATH_MIME = "application/x-fused-path";

/** The last segment of a path, on either separator — `pickFile` hands back a
 *  NATIVE path, so a Windows one has no "/" to split on. */
export function basename(path: string): string {
  const at = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return (at >= 0 ? path.slice(at + 1) : path) || path;
}

/** The name to SHOW for a path: the basename, minus the `<ms>-<n>-` stamp
 *  `saveDropped` puts on an uploaded copy — the reader dropped `clip.mp4`,
 *  not `1791894474847-2-clip.mp4`. The full path stays in the tooltip. */
export function displayName(path: string): string {
  return basename(path).replace(/^\d{10,}-\d+-/, "");
}

function extension(name: string): string {
  const base = basename(name);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/** What a file IS to this stage, by its extension and nothing else. */
export function mediaKindOf(name: string): MediaKind {
  const ext = extension(name);
  if (PICTURE_TYPES.includes(ext)) return "picture";
  if (AUDIO_TYPES.includes(ext)) return "audio";
  if (VIDEO_TYPES.includes(ext)) return "video";
  if (TEXT_TYPES.includes(ext)) return "text";
  return "other";
}

/** The extensions this model's Files mode takes, off the server's flags. */
export function acceptedTypes(flags: { audio: boolean; video: boolean }): string[] {
  return [
    ...PICTURE_TYPES,
    ...(flags.audio ? AUDIO_TYPES : []),
    ...(flags.video ? VIDEO_TYPES : []),
  ];
}

/** One sentence for a file the Files mode will not take, or null. The kind is
 *  named because the fix differs by kind: a text file has a mode of its own,
 *  a sound file wants a different model, a `.pdf` is a conversion step. */
export function refusal(name: string, types: string[]): string | null {
  if (types.includes(extension(name))) return null;
  const kind = mediaKindOf(name);
  const base = basename(name);
  if (kind === "text") {
    return `${base} is a text file — switch to Lines and drop it there, one line per entry.`;
  }
  if (kind === "audio" || kind === "video") {
    return `${base} is ${kind === "audio" ? "a sound file" : "a video"}, which this model has no encoder for — EmbeddingGemma 2 embeds sound and video.`;
  }
  return `${base} is not a picture, sound file or video (documents like PDFs need converting to text or page images first).`;
}

/** The paths a drag carries, when it carries paths at all: the app's own
 *  Explorer sets `application/x-fused-path`; a file manager on Linux (and
 *  some Mac drags) sets `text/uri-list` with `file://` URLs. Finder sets
 *  neither — its files arrive as bytes, see `saveDropped`. */
export function pathsFromDrop(data: DataTransfer): string[] {
  const out: string[] = [];
  const own = data.getData(FUSED_PATH_MIME);
  if (own) out.push(...own.split("\n").map((p) => p.trim()).filter(Boolean));
  const uris = data.getData("text/uri-list");
  if (uris) {
    for (const line of uris.split("\n")) {
      const uri = line.trim();
      if (!uri || uri.startsWith("#") || !uri.startsWith("file://")) continue;
      try {
        out.push(decodeURIComponent(uri.replace(/^file:\/\/(localhost)?/, "")));
      } catch {
        /* a malformed escape: not a path, skip it */
      }
    }
  }
  return out;
}

/** `incoming` folded into `current`: duplicates skipped, the cap honoured.
 *  Returns how many did NOT fit so the stage can say so once rather than
 *  silently dropping the tail of a big selection. */
export function mergePaths(current: string[], incoming: string[], max: number): { next: string[]; over: number } {
  const next = [...current];
  let over = 0;
  for (const path of incoming) {
    if (next.includes(path)) continue;
    if (next.length >= max) {
      over += 1;
      continue;
    }
    next.push(path);
  }
  return { next, over };
}

/** Where a dropped Finder file is written: the app's cache, in this stage's
 *  own folder, stamped so two drops of `clip.mp4` do not overwrite each
 *  other — `saveToCache` (webcam.tsx) for the image stage, restated here
 *  because the folder is this stage's. */
let dropSerial = 0;

export async function saveDropped(file: File): Promise<string> {
  const config = await getConfig();
  await mkdir(config.cache_dir).catch(() => {});
  const dir = `${config.cache_dir}/embed-playground`;
  await mkdir(dir).catch(() => {});
  // Milliseconds plus a per-session counter: two `clip.mp4`s in one drop
  // arrive within the same second (and can within the same millisecond), and
  // a second-precision stamp would upload both to one path — the later bytes
  // overwrite the earlier, and the dedupe then keeps one row for two files.
  dropSerial += 1;
  const stamp = `${Date.now()}-${dropSerial}`;
  const safe = basename(file.name).replace(/[^\w.-]+/g, "_") || "file";
  const path = `${dir}/${stamp}-${safe}`;
  await uploadFile(path, file, safe);
  return path;
}

/** The lines a dropped TEXT file contributes to the Lines corpus: one per
 *  non-empty line, trimmed. */
export function linesFromText(text: string): string[] {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}
