// A question or approval NOBODY ANSWERED, found in the transcript.
//
// The permission server (permission_server.py `_unanswered`) ends a request no
// human decided — the hour's wait ran out, the reply ended, the run moved on —
// with a deny whose text carries a trailing `[fused:unanswered reason=… ]`
// marker. The CLI writes that text as the call's tool_result, so the marker is
// on the tool segment's `output` in both transcript shapes, and matching it
// (not the English sentence) keeps the notice alive across rewordings. A
// human's own deny and a cancelled turn carry no marker.
import { ANSWERABLE_TOOL, toolChipSummary } from "./summaries";
import type { Segment, ToolSegment } from "./types";

export type UnansweredReason = "timeout" | "expired" | "abandoned";

export interface UnansweredNotice {
  reason: UnansweredReason;
  /** The headline: what went unanswered and why. */
  title: string;
  /** What was asked — the question texts, or the gated call's own summary. */
  detail: string[];
}

const MARKER = /\[fused:unanswered reason=(timeout|expired|abandoned)(?: minutes=(\d+))?\]/;

const WHY: Record<UnansweredReason, string> = {
  timeout: "timed out",
  expired: "the reply ended first",
  abandoned: "the run moved on first",
};

/** The notice for one segment, or null when it is not an unanswered call. */
export function unansweredOf(seg: Segment | null | undefined): UnansweredNotice | null {
  if (!seg || seg.kind !== "tool") return null;
  const hit = MARKER.exec(typeof seg.output === "string" ? seg.output : "");
  if (!hit) return null;
  const reason = hit[1] as UnansweredReason;
  const asked = seg.name === ANSWERABLE_TOOL;
  const why = reason === "timeout" && hit[2] ? `timed out after ${hit[2]} min` : WHY[reason];
  return {
    reason,
    title: `Unanswered ${asked ? "question" : "approval"} — ${why}`,
    detail: asked ? questionsOf(seg) : [toolChipSummary(seg)],
  };
}

/** Every unanswered call among these segments, in order. */
export function unansweredNotices(segments: Segment[] | undefined): UnansweredNotice[] {
  const out: UnansweredNotice[] = [];
  for (const seg of segments ?? []) {
    const n = unansweredOf(seg);
    if (n) out.push(n);
  }
  return out;
}

function questionsOf(seg: ToolSegment): string[] {
  const qs = (seg.input as { questions?: unknown }).questions;
  if (!Array.isArray(qs)) return [];
  return qs
    .map((q) => (q && typeof (q as { question?: unknown }).question === "string" ? (q as { question: string }).question : ""))
    .filter(Boolean);
}
