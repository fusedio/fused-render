// The status line (T:14782-14943).
//
// ONE PLAIN SENTENCE a person who has never heard of tokens can read: what
// Claude is doing right now, in everyday words. The specifics (which command,
// which file) go in the brackets after it, and only one of them. An earlier cut
// stacked tokens, input bytes, a thinking estimate, a task count and a silence
// timer in there — a dashboard, not a status (Akshil, 2026-08-28). Everything
// that was a NUMBER is gone except the clock; what was a number is now a word.
import { useEffect, useRef, useState } from "react";

import { cn } from "@platform/lib/utils";

import type { RunStatus, Working } from "../protocol/controller-api";
import type { Activity, RetryInfo } from "../protocol/types";

/** T:11803 — the model's turn has no useful noun, so it gets a mood. */
export const VERBS = [
  "Thinking",
  "Pondering",
  "Vibing",
  "Noodling",
  "Brewing",
  "Simmering",
  "Mulling",
  "Percolating",
];

/** Past this many seconds of nothing new the verb itself says the wait is real,
 *  instead of appending a silence timer to a verb that still claims progress. */
const QUIET_SECS = 20;

/** T:14782-14791 — what a LIVE api_retry says. The status code picks the words,
 *  because 529 and 429 send the user looking in different places: the API is
 *  swamped and waiting helps, versus we are being throttled. Plain words, not
 *  API vocabulary. */
export function retryVerb(retry: RetryInfo): string {
  const what =
    retry.status === 429
      ? "Claude is busy"
      : retry.status === 529
        ? "Claude's servers are busy"
        : "Connection problem";
  const budget = retry.max_retries ? "/" + retry.max_retries : "";
  return what + " — retrying (" + retry.attempt + budget + ")";
}

export interface VerbStats {
  phase: string;
  activity: Activity | null;
}

/** T:14806-14846 — the verb for a run this page owns, from the poll's phase and
 *  `activity`. `quietSecs` is how long the poll has reported nothing new. */
export function activityVerb(stats: VerbStats, thinkVerb: string, quietSecs: number): string {
  const act = stats.activity;
  if (act?.hook) return "Running project setup";
  if (stats.phase === "tooling") {
    const t = act?.tool;
    if (!t) return "Finishing a step";
    const streaming = !!act?.tool_input_bytes && !t.detail;
    switch (t.name) {
      case "Bash":
        return streaming ? "Preparing a command" : "Running a command";
      case "Read":
        return "Reading a file";
      case "Edit":
      case "MultiEdit":
      case "NotebookEdit":
        return "Editing a file";
      case "Write":
        return "Writing a file";
      case "Grep":
      case "Glob":
        return "Searching files";
      case "Task":
      case "Agent":
        return "Running a helper agent";
      case "Skill":
        return "Loading a skill";
      case "WebFetch":
        return "Fetching a page";
      case "WebSearch":
        return "Searching the web";
      // MCP tools arrive as mcp__server__tool — the last segment is the name a
      // person would recognize; the raw id is nobody's vocabulary.
      default:
        return "Using " + String(t.name).split("__").pop()?.replace(/_/g, " ");
    }
  }
  // A long silence in the model's own turn is the one ambiguity worth naming —
  // and if a background command is still running, that is almost always what
  // the wait is. "Waiting for a reply" read as waiting for the USER's reply
  // (Akshil, 2026-09-02): the words must say who owes whom.
  if (quietSecs >= QUIET_SECS) {
    return act?.tasks?.length
      ? "Waiting for a background command to finish"
      : "Claude is taking longer than usual";
  }
  if (stats.phase === "requesting") return "Sending to Claude";
  if (stats.phase === "composing") return "Claude is replying";
  return thinkVerb;
}

/** T:14848-14855 — the ONE thing that goes in the brackets beside the time.
 *  Nothing while the model itself is working: there is no useful noun for that. */
export function activityDetail(stats: VerbStats): string {
  const act = stats.activity;
  if (act?.hook) return "";
  if (stats.phase === "tooling" && act?.tool?.detail) return act.tool.detail;
  return "";
}

export interface WorkingLineProps {
  working: Working;
  status: RunStatus;
  onStop: () => void;
  /** Test seam. */
  now?: () => number;
}

export function WorkingLine({ working, status, onStop, now = Date.now }: WorkingLineProps) {
  // One mood per line, drawn when it appears rather than per second.
  const thinkVerb = useRef(VERBS[Math.floor(Math.random() * VERBS.length)]);
  // When the poll last told us something NEW. The stream rows carry no clock,
  // so this is the page's own: a line whose facts have not moved for a while
  // says so, instead of letting the timer imply progress.
  const lastKey = useRef("");
  const lastChange = useRef(now());
  const key = JSON.stringify([working.tokens, working.phase, working.retry, working.activity]);
  if (key !== lastKey.current) {
    lastKey.current = key;
    lastChange.current = now();
  }

  // BOTH writers of this line are timers, and a browser clamps timers hard in a
  // hidden or occluded window — so they stall together and the line keeps
  // whatever second it last drew ("the clock stopped but the run didn't"). One
  // draw on the way back is the whole repair: `secs` is recomputed from
  // `startedAt` every draw, never incremented, so it snaps to the truth.
  const [, tick] = useState(0);
  useEffect(() => {
    const redraw = () => tick((n) => n + 1);
    const timer = window.setInterval(redraw, 1000);
    const onVis = () => {
      if (!document.hidden) redraw();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const stopping = status === "stopping";
  // A run owned by another tab or host: no process here to kill, and a "stop"
  // that quietly did nothing would be the worst control on the page (D415).
  const fixedVerb = working.external ? "Running outside this app" : "";
  const stoppable = !working.external;
  const secs = Math.round((now() - working.startedAt) / 1000);
  const quiet = Math.round((now() - lastChange.current) / 1000);
  const stats: VerbStats = { phase: working.phase, activity: working.activity };

  const verb =
    (stopping
      ? "Stopping"
      : fixedVerb
        ? fixedVerb
        : // A live retry outranks every phase but the user's own stop: it is the
          // only one of these that explains why nothing is happening.
          working.retry
          ? retryVerb(working.retry)
          : working.phase === "awaiting"
            ? "Waiting for your approval"
            : activityVerb(stats, thinkVerb.current, quiet)) + "…";

  const parts = [secs + "s"];
  const detail = stopping || fixedVerb ? "" : activityDetail(stats);
  if (detail) parts.push(detail);

  return (
    <div
      className={cn("turn", "working", !stopping && working.phase === "awaiting" && "awaiting")}
      role="status"
    >
      <span className="star" aria-hidden="true">
        ✻
      </span>
      <span className="verb">{verb}</span>
      <span className="meta">{" (" + parts.join(" · ") + ")"}</span>
      {stoppable ? (
        <button
          type="button"
          className="stop"
          title="Stop this turn"
          disabled={stopping}
          onClick={onStop}
        >
          stop
        </button>
      ) : null}
    </div>
  );
}

export default WorkingLine;
