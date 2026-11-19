// The payload of a "show" card (platform/ui/ViewModal): pure, so the chat protocol can read it without the UI.

/** What a `show` tool put in the chat: `path` is the node's file (or the canvas folder), `canvas` / `env` the canvas
 *  it belongs to. */
export interface ViewRef { kind: "canvas" | "udf" | "widget" | "html" | "note" | (string & {}); path: string; title: string; canvas: string; env: string }

/** A usable `view` payload, or null: the row is the server's, but an old or hand-edited events file is not trusted to be whole. */
export function viewOf(raw: unknown): ViewRef | null {
  const v = raw as Partial<ViewRef> | null | undefined;
  if (!v || typeof v !== "object" || typeof v.path !== "string" || !v.path.startsWith("/") || typeof v.kind !== "string") return null;
  return { kind: v.kind, path: v.path, title: typeof v.title === "string" && v.title ? v.title : v.path.split("/").pop() || v.path,
    canvas: typeof v.canvas === "string" ? v.canvas : "", env: typeof v.env === "string" ? v.env : "" };
}
