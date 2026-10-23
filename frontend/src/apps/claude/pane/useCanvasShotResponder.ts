// THE CANVAS-SCREENSHOT PULL CHANNEL: the model's `canvas_screenshot` tool
// call parks a request (agent `_canvas_shot_requests`, surfaced on every poll
// as `state.canvasShot`), and the page that can photograph the canvas answers
// it — through the same `CanvasShot` the chat's own Screenshot seat uses
// (`shots/canvas-shot`), the picture uploaded into the shots dir and its path
// handed back (`answerCanvasShot`). The tool then returns the bytes inline.
//
// TWO KINDS OF PAGE MAY SEE A ROW. A canvas page (`shot` set) answers at
// once. A page with no canvas — the Tasks peek on the same run, a plain file
// chat — answers only after the canvas page has had its chance
// (CANVAS_SHOT_WAIT_POLLS, ~2 s), and then with the sentence that tells the
// model what to ask the user for, so a blocked tool call never waits the
// full server timeout when nobody can take the picture.
import { useEffect, useRef } from "react";
import { uploadFile } from "@platform/lib/api";
import type { CanvasShotRow } from "../protocol/types";
import type { CanvasShot } from "../shots/canvas-shot";
import { shotJoin, shotStamp, shotsDir } from "../shots/dir";
import { shotExt } from "../shots/encode";

/** How old (seconds, by the request's own `created_at`) a request must be
 *  before a page WITHOUT a canvas answers "open the canvas": the canvas page's
 *  whole budget — 40 s for its frame to come up plus 45 s for the capture —
 *  so a slow real picture is never beaten to the latch by a peek's refusal.
 *  Under the server's 100 s timeout, so the sentence still arrives first. */
export const CANVAS_SHOT_WAIT_S = 88;

/** The pause before the one retry of a capture the workbench refused. */
export const CAPTURE_RETRY_MS = 1500;

export const NO_CANVAS_SENTENCE =
  "no canvas page is open in Fused Render for this chat — ask the user to open the canvas, then try again";

export interface CanvasShotResponderOptions {
  rows: readonly CanvasShotRow[];
  /** The page's capture, when this page is a canvas page; null elsewhere. */
  shot: CanvasShot | null;
  /** Resolves true once the answer is on record; false when it must be
   *  retried (the controller un-claims the id and the row re-surfaces). */
    answer(
    id: string,
    answer: { image: string; mime: string; udf?: string } | { error: string },
  ): Promise<boolean>;
}

/** Take the picture the row asks for and put it where the tool can read it:
 *  the shots dir, under a stamped name, like every pane screenshot. */
type Answer = { image: string; mime: string; udf?: string } | { error: string };

export async function takeCanvasShot(shot: CanvasShot, row: CanvasShotRow): Promise<Answer> {
  // A UDF picture needs a node: the one the model named, or — on a node's own
  // chat (`targets` offers "udf") — the node this chat is on. The canvas chat
  // with no name has nothing to point at, and says so rather than guessing.
  let what: "canvas" | "udf" = "canvas";
  if (row.what === "udf") {
    if (!row.udf && !shot.targets.includes("udf")) {
      return { error: "name the UDF (`udf`) to screenshot one node's output from the canvas chat; without a name only `what: \"canvas\"` is possible here" };
    }
    what = "udf";
  }
  let a = await shot.capture(what, row.udf || undefined);
  if (!a.ok && /could not be captured/.test(a.error)) {
    // The workbench's html-to-image draws nothing once in a while — seen
    // right after another capture restored the DOM. One more try, a beat
    // later; a second refusal is the answer.
    await new Promise((res) => setTimeout(res, CAPTURE_RETRY_MS));
    a = await shot.capture(what, row.udf || undefined);
  }
  if (!a.ok) return { error: a.error };
  try {
    const dir = await shotsDir();
    const path = shotJoin(dir, shotStamp() + (what === "udf" ? "-udf" : "-canvas") + shotExt(a.blob));
    await uploadFile(path, a.blob, path.slice(path.lastIndexOf("/") + 1));
    return { image: path, mime: a.blob.type || "image/png", ...(row.udf ? { udf: row.udf } : {}) };
  } catch (err) {
    return { error: "the picture was taken but could not be saved (" + (err instanceof Error ? err.message : String(err)) + ")" };
  }
}

export function useCanvasShotResponder(opts: CanvasShotResponderOptions): void {
  const claimed = useRef<Set<string>>(new Set());
  /** A picture taken whose answer did not reach the server: re-answered from
   *  here when the row re-surfaces, never re-captured. */
  const taken = useRef<Map<string, Answer>>(new Map());
  const live = useRef(opts);
  live.current = opts;
  // Re-run per poll (`pollsSeen` advances), so the canvas-less fall-through
  // can count, and when `shot` arrives (the frame came up after the request).
  const rowsKey = opts.rows.map((r) => `${r.id}:${r.pollsSeen ?? 0}`).join(",");
  const ready = !!opts.shot;
  useEffect(() => {
    const { rows, shot, answer } = live.current;
    const deliver = (id: string, result: Answer) => {
      taken.current.set(id, result);
      void answer(id, result).then(
        (ok) => {
          if (ok) taken.current.delete(id);
          else claimed.current.delete(id); // the next poll re-surfaces the row; re-answer from `taken`
        },
        () => {
          claimed.current.delete(id);
        },
      );
    };
    for (const row of rows) {
      if (claimed.current.has(row.id)) continue;
      const held = taken.current.get(row.id);
      if (held) {
        claimed.current.add(row.id);
        deliver(row.id, held);
        continue;
      }
      if (!shot) {
        if (Date.now() / 1000 - (row.created_at || 0) < CANVAS_SHOT_WAIT_S) continue;
        claimed.current.add(row.id);
        deliver(row.id, { error: NO_CANVAS_SENTENCE });
        continue;
      }
      // CLAIMED, THEN RUN TO THE END — never tied to this effect's lifetime.
      // The effect re-runs on every poll (the row's count advances), and a
      // capture takes seconds; a cleanup-cancelled answer would leave the
      // tool call blocked for its whole timeout with the picture in hand.
      claimed.current.add(row.id);
      void takeCanvasShot(shot, row).then(
        (result) => deliver(row.id, result),
        (err: unknown) =>
          deliver(row.id, { error: err instanceof Error ? err.message : String(err) }),
      );
    }
  }, [rowsKey, ready]);
}
