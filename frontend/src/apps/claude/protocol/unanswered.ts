// A question or approval NOBODY ANSWERED, found in the transcript.
//
// The permission server (permission_server.py `_unanswered`) ends a request no
// human decided — the hour's wait ran out, the reply ended, the run moved on —
// with a deny whose text carries a trailing `[fused:unanswered reason=… ]`
// marker. The CLI writes that text as the call's tool_result, so the marker is
// on the tool segment's `output` in both transcript shapes, and matching it
// (not the English sentence) keeps the notice alive across rewordings. A
// human's own deny and a cancelled turn carry no marker.
import { oneLine, SUMMARY_LINE_MAX } from "@platform/lib/one-line";
import { ANSWERABLE_TOOL, PLAN_TOOL, prettyToolName, questionModel, toolChipSummary } from "./summaries";
import type { Segment, ToolSegment } from "./types";

export type UnansweredReason = "timeout" | "expired" | "abandoned";

export interface UnansweredNotice {
  reason: UnansweredReason;
  /** The headline: what went unanswered and why. */
  title: string;
  /** What was asked — the question texts, or the gated call's own summary. */
  detail: string[];
}

// The permission server appends the marker as the LAST thing in the deny text,
// so it is anchored to the end: a tool that merely prints the marker (a grep, a
// diff, a cat of this repo's tests) does not match.
const MARKER = /\[fused:unanswered reason=(timeout|expired|abandoned)(?: minutes=(\d+))?\]\s*$/;

const PHRASE: Record<UnansweredReason, string> = {
  timeout: "timed out",
  expired: "the reply ended",
  abandoned: "the run moved on",
};

/** The reason a settled card row was never answered by a person, or null when it
 *  was (an allow) or a person refused it. `expired` is its own decision; the
 *  others ride on a deny's `reason`. */
export function unansweredReasonOf(row: { decision?: string; reason?: string }): UnansweredReason | null {
  if (row.decision === "expired") return "expired";
  return row.reason === "timeout" || row.reason === "abandoned" ? row.reason : null;
}

/** A settled card's status line for an unanswered ask: "◦ Unanswered — timed out
 *  before you decided". `verb` is what the card asked of the reader. */
export function unansweredCardText(reason: UnansweredReason, verb: "decided" | "answered"): string {
  return `◦ Unanswered — ${PHRASE[reason]} before you ${verb}`;
}

/** The output a tool chip shows: the model-facing text without the trailing
 *  unanswered marker, which is plumbing for the notice and not something the
 *  reader should see. */
export function stripUnansweredMarker(output: string | null | undefined): string | null | undefined {
  return typeof output === "string" ? output.replace(MARKER, "").trimEnd() : output;
}

/** The notice for one segment, or null when it is not an unanswered call. */
export function unansweredOf(seg: Segment | null | undefined): UnansweredNotice | null {
  if (!seg || seg.kind !== "tool" || seg.status !== "error") return null;
  const hit = MARKER.exec(typeof seg.output === "string" ? seg.output : "");
  if (!hit) return null;
  const reason = hit[1] as UnansweredReason;
  const kind = seg.name === ANSWERABLE_TOOL ? "question" : seg.name === PLAN_TOOL ? "plan" : "approval";
  const why = reason === "timeout" ? (hit[2] ? `timed out after ${hit[2]} min` : PHRASE.timeout) : `${PHRASE[reason]} first`;
  return {
    reason,
    title: `Unanswered ${kind} — ${why}`,
    detail: kind === "question" ? questionsOf(seg) : kind === "plan" ? [] : [approvalLine(seg)],
  };
}

/** Every unanswered call among these segments, in order. */
export function unansweredNotices(segments: Segment[] | undefined): UnansweredNotice[] {
  const out: UnansweredNotice[] = [];
  for (const seg of segments ?? []) {
    const n = unansweredOf(seg);
    if (n) out.push(n);
    // A subagent's own transcript: its questions and approvals go unanswered too.
    if (seg.kind === "tool" && seg.children) out.push(...unansweredNotices(seg.children));
  }
  return out;
}

/** "Bash: rm -rf build" — one line, the summary capped like a task row's caption;
 *  the name alone when the call has no summary. */
function approvalLine(seg: ToolSegment): string {
  const name = prettyToolName(seg.name);
  const summary = oneLine(toolChipSummary(seg), SUMMARY_LINE_MAX);
  return summary ? `${name}: ${summary}` : name;
}

function questionsOf(seg: ToolSegment): string[] {
  return questionModel(seg.input)
    .questions.map((q) => oneLine(q.question, SUMMARY_LINE_MAX))
    .filter(Boolean);
}
