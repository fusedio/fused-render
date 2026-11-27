// The payload of a "show" card (platform/ui/ViewModal): pure, so the chat protocol can read it without the UI.

/** What a `show` tool put in the chat: `path` is the node's file (or the canvas folder), `canvas` / `env` the canvas
 *  it belongs to. */
export interface ViewRef { kind: "canvas" | "udf" | "widget" | "html" | "note" | (string & {}); path: string; title: string; canvas: string; env: string }

// An absolute path on any host the server runs on: POSIX `/x`, a Windows drive path (`C:\x`, `C:/x`) or a UNC
// path (`\\host\x`). The server sends its own native path, so a POSIX-only check drops every card on Windows.
const ABS_RE = /^(?:\/|[A-Za-z]:[\\/]|\\\\)/;

/** A usable `view` payload, or null: the line is the server's, but a transcript is not trusted to be whole. */
export function viewOf(raw: unknown): ViewRef | null {
  const v = raw as Partial<ViewRef> | null | undefined;
  if (!v || typeof v !== "object" || typeof v.path !== "string" || !ABS_RE.test(v.path) || typeof v.kind !== "string") return null;
  return { kind: v.kind, path: v.path, title: typeof v.title === "string" && v.title ? v.title : v.path.split(/[\\/]/).filter(Boolean).pop() || v.path,
    canvas: typeof v.canvas === "string" ? v.canvas : "", env: typeof v.env === "string" ? v.env : "" };
}
