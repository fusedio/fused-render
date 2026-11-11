// THE CANVAS PAGE ANSWERS THE `canvas_screenshot` TOOL (D1471, F1) — for any
// chat on its canvas, on any surface: the side pane, a node's pane, the rail's
// card, the Tasks page, a phone. Before this only a chat holding a `CanvasShot`
// could answer, and every other surface waited 88 s for "no canvas page is
// open" while the canvas sat on screen (Aman, 2026-11-11).
//
// The page polls `canvas_shot_inbox` (1 s, 3 s hidden), which is also its
// heartbeat: the permission server reads it to know a page is open (else the
// open-canvas ask). Two pages on one canvas never both capture — each CLAIMS a
// request first (`canvas_shot_claim`, O_EXCL), and claims later the further
// back it is: a focused page at once, a visible one 0.5 s on, a hidden one 2 s
// on, so the front page wins. Claimed requests run one at a time, in order:
// the page's capture is one slot (`requestCanvasShot`), and a queue never
// answers "already being taken".
import { useEffect, useRef } from "react";

import { uploadFile } from "@platform/lib/api";
import type { CanvasShot } from "@apps/claude";
import { runAgent } from "@apps/claude/protocol/agent";
import type { CanvasShotInboxRow, CanvasShotRow } from "@apps/claude/protocol/types";
import { shotExt, shotJoin, shotStamp, shotsDir } from "@apps/claude/shots";

/** The pause before the one retry of a capture the workbench refused… */
export const CAPTURE_RETRY_MS = 1500;
/** …and how far into the request a retry may still start. */
export const CAPTURE_RETRY_BUDGET_MS = 40_000;

/** How long the tool waits for one picture (agent.CANVAS_SHOT_WAIT, the
 *  permission server's CANVAS_SHOT_TIMEOUT). Past it the agent already has its
 *  timeout sentence, so a capture started then could never be delivered. */
export const SHOT_WAIT_MS = 100_000;

/** Has the tool's own wait for this request run out? The server's
 *  `deadline` when it sent one (it moves when the open-canvas ask is
 *  answered, D1471), else `created_at` plus the wait. */
export function requestExpired(row: Pick<CanvasShotInboxRow, "created_at" | "deadline">, nowMs: number): boolean {
  if (row.deadline) return nowMs >= row.deadline * 1000;
  return !!row.created_at && nowMs >= row.created_at * 1000 + SHOT_WAIT_MS;
}

export const PAGE_POLL_MS = 1000;
export const PAGE_POLL_HIDDEN_MS = 3000;
// (the permission server counts a page open for CANVAS_PAGE_FRESH, which must
// outlast a few delayed polls of this hidden pace)

/** How long after a request a page of this standing may claim it. */
export const CLAIM_DELAY_MS = { focused: 0, visible: 500, hidden: 2000 } as const;

export const HIDDEN_SENTENCE =
  "Fused Render's window is minimized or hidden — ask the user to bring it back, then try again";

export function workbenchErrorSentence(error: string): string {
  return `the canvas page cannot load the workbench (${error}) — ask the user to sign in, then try again`;
}

export interface PageStanding {
  visible: boolean;
  focused: boolean;
}

export function claimDelayMs(p: PageStanding): number {
  if (!p.visible) return CLAIM_DELAY_MS.hidden;
  return p.focused ? CLAIM_DELAY_MS.focused : CLAIM_DELAY_MS.visible;
}

/** May this page claim `row` now? The request's own clock (`created_at`,
 *  epoch seconds) is the one every page reads, so the order holds whatever
 *  phase each page polls at. */
export function claimDue(row: Pick<CanvasShotRow, "created_at">, p: PageStanding, nowMs: number): boolean {
  return nowMs >= (row.created_at || 0) * 1000 + claimDelayMs(p);
}

export type ShotAnswer =
  | { image: string; mime: string; udf?: string; framing?: string; note?: string }
  | { error: string; refusal?: boolean };

/** Take the picture the row asks for and put it where the tool can read it:
 *  the shots dir, under a stamped name, like every pane screenshot. */
export async function takeCanvasShot(shot: CanvasShot, row: Pick<CanvasShotRow, "what" | "udf">): Promise<ShotAnswer> {
  // A UDF picture needs a node. A node's chat has it filled in by the server
  // (its node); the canvas chat with no name has nothing to point at.
  let what: "canvas" | "udf" = "canvas";
  if (row.what === "udf") {
    if (!row.udf && !shot.targets.includes("udf")) {
      return { error: "name the UDF (`udf`) to screenshot one node's output from the canvas chat; without a name only `what: \"canvas\"` is possible here", refusal: true };
    }
    what = "udf";
  }
  const began = Date.now();
  let a = await shot.capture(what, row.udf || undefined, "agent");
  // The workbench's html-to-image draws nothing once in a while, and the host
  // refuses while the user's own Screenshot seat has a capture out: one more
  // try, a beat later, while it still fits the budget.
  if (!a.ok && /could not be captured|already being taken/.test(a.error) && Date.now() - began < CAPTURE_RETRY_BUDGET_MS) {
    await new Promise((res) => setTimeout(res, CAPTURE_RETRY_MS));
    a = await shot.capture(what, row.udf || undefined, "agent");
  }
  if (!a.ok) return a.refusal ? { error: a.error, refusal: true } : { error: a.error };
  try {
    const dir = await shotsDir();
    const path = shotJoin(dir, shotStamp() + (what === "udf" ? "-udf" : "-canvas") + shotExt(a.blob));
    await uploadFile(path, a.blob, path.slice(path.lastIndexOf("/") + 1));
    return {
      image: path,
      mime: a.blob.type || "image/png",
      ...(row.udf ? { udf: row.udf } : {}),
      ...(a.framing ? { framing: a.framing } : {}),
      ...(a.note ? { note: a.note } : {}),
    };
  } catch (err) {
    // Not the window's fault: never swapped for the hidden-window sentence.
    return { error: "the picture was taken but could not be saved (" + (err instanceof Error ? err.message : String(err)) + ")", refusal: true };
  }
}

export interface PageState extends PageStanding {
  frameUp: boolean;
  /** The page's own load error (signed out, whoami), or "". */
  error: string;
}

export interface PageResponderDeps {
  page(): PageState;
  inbox(page: PageState): Promise<CanvasShotInboxRow[]>;
  claim(row: CanvasShotInboxRow): Promise<boolean>;
  take(row: CanvasShotInboxRow): Promise<ShotAnswer>;
  /** True once the answer is on record (or can never be); false to retry. */
  answer(row: CanvasShotInboxRow, answer: ShotAnswer): Promise<boolean>;
  now(): number;
}

/** The responder without React: `tick()` is one poll. */
export function createPageShotResponder(deps: PageResponderDeps) {
  const busy = new Set<string>();
  /** Answered (or unanswerable): never captured again, whatever the inbox
   *  says before the server drops the row. */
  const done = new Set<string>();
  /** A picture whose answer did not land: re-answered, never re-captured. */
  const taken = new Map<string, ShotAnswer>();
  let chain: Promise<void> = Promise.resolve();
  /** The page is leaving: claim nothing more, start no capture. A claim it
   *  holds lapses once its heartbeat goes stale (agent._canvas_claim_holder). */
  let stopped = false;

  const answerFor = async (row: CanvasShotInboxRow): Promise<ShotAnswer> => {
    const p = deps.page();
    if (p.error && !p.frameUp) return { error: workbenchErrorSentence(p.error) };
    const a = await deps.take(row);
    // A hidden page with its workbench up could not take it because of the
    // window; a frame that never came up keeps its own sentence.
    const after = deps.page();
    if ("error" in a) {
      if (!a.refusal && !after.visible && after.frameUp) return { error: HIDDEN_SENTENCE };
      return { error: a.error };
    }
    return a;
  };

  const run = (row: CanvasShotInboxRow) => {
    busy.add(row.id);
    chain = chain.then(async () => {
      if (stopped && !taken.has(row.id)) {
        busy.delete(row.id);
        return;
      }
      // Queued behind slow captures until the tool's wait ran out: nothing
      // can be delivered, so skip it rather than spend a capture on it.
      if (requestExpired(row, deps.now())) {
        taken.delete(row.id);
        done.add(row.id);
        busy.delete(row.id);
        return;
      }
      let a = taken.get(row.id);
      if (!a) {
        try {
          a = await answerFor(row);
        } catch (err) {
          a = { error: err instanceof Error ? err.message : String(err) };
        }
        taken.set(row.id, a);
      }
      let ok = false;
      try {
        ok = await deps.answer(row, a);
      } catch {
        ok = false;
      }
      if (ok) {
        taken.delete(row.id);
        done.add(row.id);
      }
      busy.delete(row.id);
    });
    return chain;
  };

  return {
    async tick(): Promise<void> {
      if (stopped) return;
      const rows = await deps.inbox(deps.page());
      for (const row of rows) {
        if (stopped) return;
        if (busy.has(row.id) || done.has(row.id)) continue;
        if (requestExpired(row, deps.now())) {
          done.add(row.id);
          continue;
        }
        if (!row.claimed && !taken.has(row.id)) {
          // Standing as of NOW, not as of the poll: a page that lost focus
          // meanwhile must not claim ahead of the one that gained it.
          if (!claimDue(row, deps.page(), deps.now())) continue;
          if (!(await deps.claim(row)) || stopped) continue;
        }
        void run(row);
      }
    },
    stop(): void {
      stopped = true;
    },
    /** Everything queued so far has settled (tests). */
    idle: () => chain,
  };
}

function newPageId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random().toString(36).slice(2);
}

/** The hook CanvasWorkspace mounts: polls while the page is up. */
export function useCanvasPageShotResponder(opts: {
  env: string;
  name: string;
  shot: CanvasShot;
  frameUp: () => boolean;
  error: string | null;
}): void {
  const live = useRef(opts);
  // Committed props only: a render React throws away must not reach a
  // capture already in flight.
  useEffect(() => {
    live.current = opts;
  });
  const { env, name } = opts;
  useEffect(() => {
    const pageId = newPageId();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const standing = (): PageState => {
      let focused = false;
      try {
        focused = document.hasFocus();
      } catch {
        /* not a real document */
      }
      return {
        visible: document.visibilityState === "visible",
        focused,
        frameUp: live.current.frameUp(),
        error: live.current.error || "",
      };
    };
    const responder = createPageShotResponder({
      page: standing,
      now: () => Date.now(),
      inbox: async (p) => {
        const out = await runAgent("canvas_shot_inbox", {
          state: JSON.stringify({ env, name, page_id: pageId, visible: p.visible, focused: p.focused, frame_up: p.frameUp, error: p.error }),
        });
        return out.rows || [];
      },
      claim: async (row) =>
        (await runAgent("canvas_shot_claim", { run_id: row.runId, request_id: row.id, state: JSON.stringify({ page_id: pageId }) })).claimed,
      take: (row) => takeCanvasShot(live.current.shot, row),
      answer: async (row, a) => {
        const out = await runAgent("canvas_shot", { run_id: row.runId, request_id: row.id, state: JSON.stringify(a) });
        return "answered" in out || !out.retry;
      },
    });
    const loop = async () => {
      try {
        await responder.tick();
      } catch {
        /* a missed poll is the next poll's */
      }
      if (stopped) return;
      timer = setTimeout(() => void loop(), document.visibilityState === "visible" ? PAGE_POLL_MS : PAGE_POLL_HIDDEN_MS);
    };
    void loop();
    return () => {
      stopped = true;
      responder.stop();
      if (timer) clearTimeout(timer);
    };
  }, [env, name]);
}
