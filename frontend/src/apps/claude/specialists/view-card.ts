// A specialist's `show` tool row as a card (docs/specialists.md §15). The tool's answer ends with one line,
// `[fused-card] {json}` (fused_render/specialists/show.py CARD), carrying what the card opens; this reads it back
// from the settled tool row. Any other row, a failed call, or a row without the line is not a card and stays a
// plain tool row.
import { viewOf, type ViewRef } from "@platform/lib/view-ref";
import type { Segment } from "../protocol/types";

const SHOW_RE = /^mcp__specialist[_-]canvas__show$/;
const CREATE_RE = /^mcp__specialist[_-]canvas__canvas_create$/;
const CARD = "[fused-card] ";

export interface ViewCardRef extends ViewRef {
  /** The specialist's one line about what this is ("" when it gave none). */
  note: string;
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
  return { ...view, note: typeof note === "string" ? note : "" };
}

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
