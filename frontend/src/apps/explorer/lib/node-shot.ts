// THE FILE VIEW PICTURES THE NODE IT SHOWS (SPEC §53.8).
//
// A node file of a cloned canvas opens as that node's own page (a node
// template: `canvas_udf`, `canvas_widget`, `canvas_note`). A chat working on
// the node asks the agent's `canvas_screenshot` tool to look at its output;
// until now only the canvas page could answer, so the user was asked to open
// the canvas for a look at something already on screen.
//
// This is the picture for that answer: the content pane's own frame, as it
// stands, taken the way the chat's Screenshot seat takes the pane. The
// responder that claims the request and delivers it is the canvas page's own
// (apps/claude/shots/canvas-page-shot-responder), mounted by Preview with
// `node` set so the server hands it that node's requests and no others.
import { capturePane, frameIsCrossOrigin } from "@apps/claude/shots/capture";
import type { CanvasShot, CanvasShotAnswer } from "@apps/claude/shots/canvas-shot";
import type { CaptureResult } from "@apps/claude/shots/types";
import { getJson } from "@platform/lib/api";

/** What GET /api/canvases/node says a file is, reduced to what a node page's
 *  responder needs. */
export interface NodeOfFile {
  env: string;
  name: string;
  node: string;
}

/** The canvas node `path` is, or null (not a node file, Canvases off, the
 *  call failed: no responder then, never an error). */
export async function nodeOfFile(path: string): Promise<NodeOfFile | null> {
  try {
    const info = await getJson<{ enabled?: boolean; env?: unknown; name?: unknown; node?: unknown }>(
      `/api/canvases/node?path=${encodeURIComponent(path)}`,
    );
    if (!info.enabled || typeof info.env !== "string" || typeof info.name !== "string" || typeof info.node !== "string") {
      return null;
    }
    return { env: info.env, name: info.name, node: info.node };
  } catch {
    return null;
  }
}

export const NESTED_BLANK_CAVEAT =
  "taken without a screen capture, so the node's result area (a frame inside the page) is BLANK in this picture: " +
  "do not read it as an empty result";

type Take = (frame: HTMLIFrameElement) => Promise<CaptureResult>;

const takePane: Take = (frame) => capturePane(frame, { xo: frameIsCrossOrigin(frame) });

/** The shot a node page offers: its one node's output, from the frame on
 *  screen. Anything else is refused in a sentence (the server does not send
 *  it other requests; this is the belt to that). */
export function nodePageShot(frame: () => HTMLIFrameElement | null, node: string, take: Take = takePane): CanvasShot {
  return {
    targets: ["udf"],
    async capture(what, udf): Promise<CanvasShotAnswer> {
      if (what !== "udf" || (udf && udf !== node)) {
        return { ok: false, error: `this page shows only "${node}", as its own file`, refusal: true };
      }
      const target = frame();
      if (!target) return { ok: false, error: `"${node}" is not on screen in the file view` };
      const shot = await take(target);
      // The object URL is for a tray's thumbnail; nothing here shows one.
      if (shot.thumb) URL.revokeObjectURL(shot.thumb);
      if (!shot.blob) {
        return { ok: false, error: `the page of "${node}" could not be captured` + (shot.why ? ` (${shot.why})` : "") };
      }
      const caveats = [`this is "${node}" as its own file shows it in the file view`, ...shot.notes];
      // A node's page draws its result inside a frame of its own (a UDF's
      // result page is sandboxed, a widget is the workbench's origin). Only
      // a capture of the screen sees into those; a redraw of this page's own
      // document leaves them blank, and the picture must say so rather than
      // pass for an empty result.
      if (shot.via === "dom") caveats.push(NESTED_BLANK_CAVEAT);
      return { ok: true, blob: shot.blob, framing: "node", note: caveats.join("; ") };
    },
  };
}
