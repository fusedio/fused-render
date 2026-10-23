// A PICTURE FROM THE CANVAS PAGE (apps/canvases/CanvasWorkspace). The
// workbench frame is cross-origin, so no chat can photograph it; the host
// relays to the frame, which captures with its own tools and hands the bytes
// back — nothing is uploaded anywhere, the picture exists only to be attached.
//
// `what` picks the picture: "canvas" is the flow graph as the viewport shows
// it; "udf" is ONE node's output (the node whose file the chat is on). A chat
// offers only the targets its host lists.
//
// Two mounts, one contract:
//   - the canvas chat is an in-page ChatMount: the host hands `requestCanvasShot`
//     over directly, targets ["canvas"];
//   - the node pane's chat is an explorer embed FRAMED by the host
//     (`_canvasshot=1`): `requestCanvasShotFromParent` below asks the parent
//     window and waits for its answer, targets ["canvas", "udf"] —
//       chat   → parent { type: "fused-canvas-shot", req, what, udf? }
//       parent → chat   { type: "fused-canvas-shot-done", req, ok, blob?, error? }
//     same origin both ways, `req` keeping a late answer off a later click.
//
// HOW THE BYTES COME BACK: `postMessage` structured-clones a Blob — the
// receiving window gets its own copy of the bytes, origin notwithstanding —
// so the frame's answer carries the picture itself, no data URL, no upload.

export type CanvasShotWhat = "canvas" | "udf";
export type CanvasShotAnswer = { ok: true; blob: Blob } | { ok: false; error: string };
export interface CanvasShot {
  /** `udf` names the node for `what: "udf"`; omitted, the host uses the node
   *  this chat is on (the agent's `canvas_screenshot` tool may name any). */
  capture(what: CanvasShotWhat, udf?: string): Promise<CanvasShotAnswer>;
  /** What this chat may ask for; one entry means a plain seat, two a menu. */
  targets: readonly CanvasShotWhat[];
}

export const CANVAS_SHOT_LABEL: Record<CanvasShotWhat, string> = {
  canvas: "Capture the canvas view",
  udf: "Capture this UDF's output",
};

/** Generous: the frame's capture is seconds on a big graph; the parent's own
 *  timer answers before this does, so this is only the belt to its braces. */
const ANSWER_TIMEOUT_MS = 60_000;

/** The framed chat's ask. */
export function requestCanvasShotFromParent(
  what: CanvasShotWhat,
  udf?: string,
): Promise<CanvasShotAnswer> {
  if (typeof window === "undefined" || window.parent === window) {
    return Promise.resolve({ ok: false, error: "this chat is not on a canvas page" });
  }
  return new Promise<CanvasShotAnswer>((resolve) => {
    const req = Date.now() + Math.random();
    let done = false;
    const finish = (answer: CanvasShotAnswer) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(answer);
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.source !== window.parent) return;
      const data = event.data;
      if (data?.type !== "fused-canvas-shot-done" || data.req !== req) return;
      if (data.ok === true && data.blob instanceof Blob) finish({ ok: true, blob: data.blob });
      else {
        finish({
          ok: false,
          error: typeof data.error === "string" ? data.error : "the canvas sent no picture",
        });
      }
    };
    const timer = setTimeout(
      () => finish({ ok: false, error: "the canvas did not answer" }),
      ANSWER_TIMEOUT_MS,
    );
    window.addEventListener("message", onMessage);
    window.parent.postMessage(
      { type: "fused-canvas-shot", req, what, ...(udf ? { udf } : {}) },
      window.location.origin,
    );
  });
}

/** The framed chat's contract: both targets, through the parent. */
export const CANVAS_SHOT_FROM_PARENT: CanvasShot = {
  capture: requestCanvasShotFromParent,
  targets: ["canvas", "udf"],
};
