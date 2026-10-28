// THE SCRIM OVER THE WORKBENCH WHILE A PICTURE IS BEING TAKEN (CanvasWorkspace
// `requestCanvasShot`). The capture runs INSIDE the cross-origin workbench
// frame (html-to-image over the flow graph: canvases swapped for images,
// result iframes overlaid, then everything restored), which takes seconds on
// a real canvas — 23 s measured on a 22-node map canvas — and photographs
// whatever the DOM is doing meanwhile. A pan, a zoom, a run or a selection
// during that window spoils the picture or races the restore, and until now
// the only sign anything was happening was the chat seat reading
// "Capturing…" a pane away: the canvas itself looked idle and interactive.
//
// So the host draws its own scrim over the frame — a wash, a spinner and one
// line naming WHO is looking and WHAT is being taken — and takes the pointer
// with it (`pointer-events: auto`, the frame underneath gets nothing). Host-
// side, not inside the workbench, so it holds against an older workbench
// build too, and so the agent's `canvas_screenshot` tool (which fires with no
// gesture from the user) is announced where the user is looking.
//
// Pure: what the label says for each phase, kept out of the component so it
// can be tested without the page.
import type { CanvasShotBy, CanvasShotWhat } from "@apps/claude";

export interface CanvasShotPhase {
  what: CanvasShotWhat;
  by: CanvasShotBy;
  /** The node named for a "udf" picture, when the asker named one. */
  udf: string | null;
  /** Still waiting for the workbench frame's first hello (`awaitFrameUp`):
   *  nothing is being captured yet, and the line should say so rather than
   *  claim a capture that has not started. */
  waitingForFrame: boolean;
}

/** The one line on the scrim. The USER's own capture is a plain progress
 *  sentence; the AGENT's names the asker, because nobody pressed anything
 *  and a canvas that locks itself unprompted has to say who asked. The
 *  asker is "your agent", not a model's name: the `canvas_screenshot` tool
 *  is every backend's (Claude, Gemini, Codex…), and the host cannot tell
 *  whose session parked the request. */
export const CANVAS_SHOT_AGENT = "Your agent";
export function canvasShotScrimLabel(phase: CanvasShotPhase): string {
  if (phase.waitingForFrame) return "Waiting for the workbench…";
  const agent = phase.by === "agent";
  if (phase.what === "udf") {
    const node = phase.udf ? phase.udf + "'s output" : "this UDF's output";
    return agent ? CANVAS_SHOT_AGENT + " is looking at " + node + "…" : "Capturing " + node + "…";
  }
  return agent ? CANVAS_SHOT_AGENT + " is looking at the canvas…" : "Capturing the canvas…";
}

/** The second line: why the canvas is not answering clicks right now. */
export const CANVAS_SHOT_SCRIM_HINT = "Hold still — moving the canvas now would spoil the picture.";
