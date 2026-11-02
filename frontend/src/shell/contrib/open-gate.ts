// The app page's open gate for a Marketplace copy (CONTRIB_LOOP.md §18.4,
// §18.5): the decisions AppPage.tsx makes before it frames the app, pulled
// out so they can be tested without mounting the page.
//
// The SERVER is the gate: opening the app (`/api/apps/entry`, `/render`)
// answers 423 while a hard block stands or a soft block waits for the user's
// yes (`blocks.gate`). The page reads that refusal first, so a soft-blocked
// release never runs before the question is asked. What the page adds on its
// own is a hold: once it knows the folder is a Marketplace copy, the frame
// waits for the copy's first status read rather than mount and be torn down
// when the read says the copy waits.
import { softBlockWaiting, type AppContribState, type CopyBlock } from "@platform/lib/contrib";

/** The block an open refusal carries: a 423 whose body is `{blocked: {...}}`
 *  (`blocks.gate`). Null for any other failure. */
export function openRefusal(err: unknown): CopyBlock | null {
  const e = err as { status?: number; body?: unknown } | null;
  if (!e || e.status !== 423) return null;
  const blocked = (e.body as { blocked?: unknown } | undefined)?.blocked;
  if (!blocked || typeof blocked !== "object") return null;
  return blocked as CopyBlock;
}

/** A refusal that asks rather than stops: the user's "Open anyway" (the
 *  server's `confirm-block`) is the way through. */
export function asksFirst(block: CopyBlock): boolean {
  return block.level === "soft";
}

/** What the Overview shows in the frame's place.
 *  `ask`: the copy's soft block waits for a yes (the strip above asks).
 *  `hold`: a Marketplace copy whose first status read is still out.
 *  `frame`: the app. */
export function overviewGate(contrib: AppContribState, marketplace: boolean): "frame" | "hold" | "ask" {
  if (softBlockWaiting(contrib.data?.panel?.block)) return "ask";
  if (marketplace && !contrib.settled) return "hold";
  return "frame";
}
