// An expert's `show` tool row as a card (docs/experts.md §14). The tool's answer ends with one line,
// `[fused-card] {json}` (fused_render/experts/show.py CARD), carrying what the card opens; this reads it back
// from the settled tool row. Any other row, a failed call, or a row without the line is not a card and stays a
// plain tool row.
import { viewOf, type ViewRef } from "@platform/lib/view-ref";
import type { Segment } from "../protocol/types";

const SHOW_RE = /^mcp__expert[_-]canvas__show$/;
const CREATE_RE = /^mcp__expert[_-]canvas__canvas_create$/;
const CARD = "[fused-card] ";

export interface ViewCardRef extends ViewRef {
  /** The expert's one line about what this is ("" when it gave none). */
  note: string;
  /** The expert let the card load by itself when it appears; false for work it judged too heavy to run unasked. */
  live: boolean;
}

export function viewCardOf(seg: Segment | null | undefined): ViewCardRef | null {
  if (!seg || seg.kind !== "tool" || seg.status !== "ok" || !SHOW_RE.test(seg.name)) return null;
  const out = typeof seg.output === "string" ? seg.output : "";
  const at = out.lastIndexOf(CARD);
  if (at < 0) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(out.slice(at + CARD.length).split("\n")[0]!);
  } catch {
    return null;
  }
  const view = viewOf(raw);
  if (!view) return null;
  const note = (raw as { note?: unknown }).note;
  return { ...view, note: typeof note === "string" ? note : "", live: (raw as { live?: unknown }).live !== false };
}

// `show` calls this window has watched run, by tool_use id. A turn can settle in the same update that settles
// its last `show`, so "the turn is still running" alone would miss a card that was born in front of the reader.
const watched = new Set<string>();

/** Note a `show` row that has not settled yet; a no-op for any other row. */
export function watchShow(seg: Segment | null | undefined): void {
  if (seg && seg.kind === "tool" && seg.id && seg.status === "running" && SHOW_RE.test(seg.name)) watched.add(seg.id);
}

/** This window saw the `show` call with this id while it was running. */
export const wasWatched = (id: string): boolean => watched.has(id);

/** The two tools as plain words for the tool row and the status line, or null for any other tool. */
export function canvasToolLabel(tool: unknown, input: Record<string, unknown> | null | undefined, done: boolean): string | null {
  const name = String(tool ?? "");
  if (CREATE_RE.test(name)) {
    const canvas = typeof input?.name === "string" && input.name ? ` ${input.name}` : "";
    return done ? `Made the canvas${canvas}` : `Making the canvas${canvas}`;
  }
  if (SHOW_RE.test(name)) return done ? "Showed a card" : "Showing a card";
  return null;
}
