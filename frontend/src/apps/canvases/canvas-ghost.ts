// THE CANVAS GHOST (SPEC §28 CV-10): what the canvas page shows while the
// workbench frame loads.
//
// The workbench is a hosted page in a cross-origin frame: it has to fetch its
// own bundle, sign in over postMessage and hydrate the canvas before it draws
// a node, and until then the page had one line of text. But the canvas is
// already on disk, in the clone's `canvas.toml`, and the canvas.toml view
// (templates/canvas/) draws it the way the workbench lays it out, from that
// file alone, in a fraction of a second. So the page frames THAT view over the
// workbench frame, in its ghost mode (`ghost=1`: no controls, nothing to
// press, fitted the way the workbench fits a canvas), and takes it away once
// the workbench says hello.
//
// Pure rules here; the element is CanvasGhost.tsx.

/** The ghost's word to the page (the view's `tellHost`). */
export const GHOST_MESSAGE = "fused-canvas-ghost";

/** How long the ghost stays after the workbench's hello before it starts to
 *  go: the hello is the workbench HYDRATED, and its first fitted paint of the
 *  canvas lands a moment after. */
export const GHOST_HOLD_MS = 350;
/** The fade itself. */
export const GHOST_FADE_MS = 250;

/** The ghost frame's address: the canvas.toml view (`view`, the page file the
 *  server's status names) on the clone's `canvas.toml`, in ghost mode. Null
 *  when either half is unknown: an older server, or no clone folder yet. */
export function ghostSrc(view: string | null | undefined, cloneDir: string | null | undefined): string | null {
  const file = ghostFile(cloneDir);
  if (!view || !file) return null;
  return "/render?path=" + encodeURIComponent(view) + "&_file=" + encodeURIComponent(file) + "&ghost=1";
}

/** The file a clone's ghost draws. */
export function ghostFile(cloneDir: string | null | undefined): string | null {
  if (!cloneDir) return null;
  return cloneDir.replace(/[\\/]+$/, "") + "/canvas.toml";
}

/** The ghost's word, when `data` is one about `file`: "ready" (drawn) or
 *  "failed" (no canvas.toml, or one that does not read). Anything else, and a
 *  word about another canvas's file, is null. */
export function ghostWord(data: unknown, file: string): "ready" | "failed" | null {
  if (!data || typeof data !== "object") return null;
  const d = data as { type?: unknown; state?: unknown; file?: unknown };
  if (d.type !== GHOST_MESSAGE || d.file !== file) return null;
  return d.state === "ready" || d.state === "failed" ? d.state : null;
}

export type GhostPhase =
  /** Framed, not shown: not drawn yet. */
  | "loading"
  /** Drawn and over the workbench frame. */
  | "shown"
  /** The workbench is up: fading out. */
  | "leaving"
  /** Not in the page at all. */
  | "gone";

/** Where the ghost stands.
 *
 *  - It is never shown before it is drawn, and never at all when it failed:
 *    a first open has no canvas.toml, and then the page's own loading line is
 *    what shows, as before.
 *  - A workbench that is up before the ghost was ever shown gets no ghost:
 *    there is nothing left to cover.
 *  - `leftAtMs` is when the leave began (hello + the hold), null before. */
export function ghostPhase(s: {
  ready: boolean;
  failed: boolean;
  /** The workbench frame has said hello. */
  up: boolean;
  /** The ghost was on screen at some point since it was framed. */
  wasShown: boolean;
  /** Ms since the workbench said hello (0 when it has not). */
  sinceUpMs: number;
}): GhostPhase {
  if (s.failed) return "gone";
  if (!s.up) return s.ready ? "shown" : "loading";
  if (!s.wasShown) return "gone";
  if (s.sinceUpMs < GHOST_HOLD_MS) return "shown";
  return s.sinceUpMs < GHOST_HOLD_MS + GHOST_FADE_MS ? "leaving" : "gone";
}
