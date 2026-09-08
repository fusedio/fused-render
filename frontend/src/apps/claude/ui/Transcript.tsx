// The scrollport (T:12752-13060).
//
// A PLAIN DIV, not `ScrollArea`: the follow-bottom rules below read
// `scrollTop`/`scrollHeight`/`clientHeight` off the scroller itself and write
// `scrollTop` back, and every one of the gesture distinctions depends on those
// being the element the browser actually scrolls.
//
// Three rules, and each exists because the obvious version of it leaked:
//   * follow is GESTURE-based and threshold-free for wheel and touch. A 20px
//     drag stays inside any "near bottom" window, so geometry read the touch as
//     re-arming the follow it was trying to break;
//   * a scroll CLAMP is not a gesture. The tail shrinks all the time (a chip
//     resolving to a smaller image, the working line leaving at run end), and
//     reading the browser's clamp as "the reader moved up" was the other way
//     the follow died mid-turn;
//   * growth is OBSERVED, not announced. Most of what lands in a live turn
//     grows AFTER the append that scrolled — a chip that expands when its
//     output arrives, a code block growing as the highlighter runs, a picture
//     that only takes up room once it loads — so a ResizeObserver answers all
//     of them with the one flag.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Skeleton } from "@platform/shadcn/ui/skeleton";

import type { ChatController, ChatState, UserTurn } from "../protocol/controller-api";
import type { PermissionMode } from "../protocol/types";
import { CardStack, type CardActions } from "./CardStack";
import { TroubleView } from "./TroubleView";
import { Turn } from "./Turn";
import { WorkingLine } from "./WorkingLine";
import "../styles/transcript.css";

/** T:12754 — how close to the tail re-arms the follow. */
const NEAR_BOTTOM_PX = 60;
/** T:12848 — long enough to be seen after the scroll settles, short enough that
 *  it is plainly a flare and not a mode. */
export const ANCHOR_FLARE_MS = 1600;
/** T:12912 — a restored turn's pictures load after their turn is on screen, and
 *  every one above the anchor slides it off. Re-centre at a few decaying points
 *  rather than continuously. */
export const ANCHOR_SETTLE_MS = [120, 400, 1000];

/** Where the typer is pointed, and what it has drawn (protocol/segments.ts
 *  `streamingTailOf` + protocol/typer.ts `TyperFrame`). */
export interface TranscriptTail {
  /** The turn the typer is attached to. */
  turnKey: string;
  /** The growing segment's index, or -1 for a turn's flat body. */
  index: number;
  text: string;
  cursor: boolean;
}

export interface TranscriptProps {
  state: ChatState;
  actions: CardActions & Pick<ChatController, "stopRun">;
  /** The live permission mode from poll — gates a perm card's escalation. */
  liveMode?: PermissionMode;
  /** The typewriter's current frame, or absent when nothing is streaming. */
  tail?: TranscriptTail | null;
  /** The picker's `permission` param, for a plan card's landing mode. */
  pickerMode?: string;
  /** `?msg=<transcript record uuid>` at boot (T:12844). */
  msgAnchor?: string | null;
  /** Fired after the one attempt, landed or not: the param comes off the URL
   *  (`null`, not `""`) so a copied address carries no dangling `&msg=`. */
  onAnchorSpent?: () => void;
  onShowSent?: (turn: UserTurn) => void;
  /** What the app was doing, for a trouble card's report. */
  what?: string;
}

export const Transcript = memo(function Transcript({
  state,
  actions,
  liveMode,
  tail,
  pickerMode,
  msgAnchor,
  onAnchorSpent,
  onShowSent,
  what,
}: TranscriptProps) {
  const port = useRef<HTMLDivElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const followTail = useRef(true);
  const [flare, setFlare] = useState<string | null>(null);
  const anchorSpent = useRef(false);
  const stopSettle = useRef<(() => void) | null>(null);

  // ── the follow flag and the gestures that move it ────────────────────────
  useEffect(() => {
    const wrap = port.current;
    if (!wrap) return;
    const nearBottom = () =>
      wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < NEAR_BOTTOM_PX;
    // A wheel/trackpad flick upward is an unambiguous "let me read", so it
    // drops the follow with NO distance threshold — that is what makes it
    // impossible for the next frame's write to out-race the gesture.
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) followTail.current = false;
    };
    // A finger dragging DOWN scrolls the content up: the same "let me read", and
    // tracked across touchmove rather than read off the scrollport, because
    // asking geometry here re-introduced exactly that race.
    let touchY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0] ? e.touches[0].clientY : null;
    };
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0] ? e.touches[0].clientY : null;
      if (y !== null && touchY !== null && y > touchY + 2) followTail.current = false;
      touchY = y;
    };
    // Everything else that moves the scrollport — scrollbar drag, keyboard, a
    // scrollIntoView, our own writes. `lastTop`/`lastHeight` tell a reader
    // moving UP apart from the browser CLAMPING scrollTop because the content
    // got shorter; a clamp is not a gesture.
    let lastTop = 0;
    let lastHeight = 0;
    const onScroll = () => {
      const top = wrap.scrollTop;
      const h = wrap.scrollHeight;
      if (h >= lastHeight && top < lastTop - 1) followTail.current = false;
      else if (nearBottom()) followTail.current = true;
      lastTop = top;
      lastHeight = h;
    };
    const followBottom = () => {
      if (followTail.current) wrap.scrollTop = wrap.scrollHeight;
    };
    wrap.addEventListener("wheel", onWheel, { passive: true });
    wrap.addEventListener("touchstart", onTouchStart, { passive: true });
    wrap.addEventListener("touchmove", onTouchMove, { passive: true });
    wrap.addEventListener("scroll", onScroll, { passive: true });
    // Belt and braces for the one growth a ResizeObserver can miss: a replaced
    // element that reserves its box up front paints into a box that never
    // changes size, and any LAYOUT its load shifts may land in the frame the
    // observer already answered. `load` does not bubble, hence the capture.
    wrap.addEventListener("load", followBottom, true);
    // Writing scrollTop cannot resize anything, so this observer cannot
    // re-enter itself.
    const grown =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(followBottom);
    if (grown && log.current) grown.observe(log.current);
    return () => {
      wrap.removeEventListener("wheel", onWheel);
      wrap.removeEventListener("touchstart", onTouchStart);
      wrap.removeEventListener("touchmove", onTouchMove);
      wrap.removeEventListener("scroll", onScroll);
      wrap.removeEventListener("load", followBottom, true);
      grown?.disconnect();
    };
  }, []);

  // A new turn is the reader asking for the tail again: sending re-arms the
  // follow that scrolling up turned off, so the answer to what they just asked
  // streams in front of them (T:13466-13470). Layout effect, so the write lands
  // in the frame the turn was painted in.
  const lastTurnKey = state.turns.length ? state.turns[state.turns.length - 1].key : "";
  const userTurns = state.turns.filter((t) => t.role === "user").length;
  useLayoutEffect(() => {
    followTail.current = true;
    if (port.current) port.current.scrollTop = port.current.scrollHeight;
  }, [userTurns]);
  useLayoutEffect(() => {
    // Everything the reader did NOT ask for goes through the flag.
    if (followTail.current && port.current) port.current.scrollTop = port.current.scrollHeight;
  }, [lastTurnKey, state.rev]);
  // An open card is a HARD BLOCK: the run cannot continue without the user, so a
  // reader who has scrolled away is waiting on something they cannot see. One of
  // the few places that scrolls unconditionally (T:14652-14663).
  //
  // KEYED ON THE IDS, not the COUNT: T scrolls per card MOUNT, and one card
  // resolving while another opens in the same poll leaves the count unchanged —
  // so the new card, which the run is blocked on, never brought the scrollport
  // to itself.
  const openCards = openCardIds(state.permissions);
  useLayoutEffect(() => {
    if (openCards && port.current) port.current.scrollTop = port.current.scrollHeight;
  }, [openCards]);

  // ── ?msg= anchor ─────────────────────────────────────────────────────────
  // EVERYTHING here degrades to silence: no param, a uuid from another
  // transcript, a uuid whose record this page does not render — each ends with
  // the chat exactly as it would have been, because landing at the bottom of
  // the right conversation is a far better failure than a throw on a stale link.
  // Read through a ref, not a dependency: the callback is rebuilt every render
  // by the host, and re-running this effect for it tore down the flare's own
  // timer while `anchorSpent` blocked ever rescheduling it — so the halo never
  // faded.
  const spend = useRef(onAnchorSpent);
  spend.current = onAnchorSpent;
  useEffect(() => {
    if (!msgAnchor || anchorSpent.current || state.historyLoading) return;
    // Spent on the first transcript it is offered, landed or not: left armed, a
    // uuid that matched nothing here would flare whichever turn of a DIFFERENT
    // conversation happened to carry that id.
    anchorSpent.current = true;
    const wrap = port.current;
    const el = findTurn(log.current, msgAnchor);
    spend.current?.();
    if (!el || !wrap) return;
    const still =
      typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollToAnchor(el, still);
    stopSettle.current = settleAnchor(wrap, el, still);
    setFlare(msgAnchor);
  }, [msgAnchor, state.historyLoading]);
  // The flare's own effect, keyed on the flare: one timer per halo, torn down
  // only when the halo it belongs to goes.
  useEffect(() => {
    if (!flare) return;
    const off = window.setTimeout(() => setFlare(null), ANCHOR_FLARE_MS);
    return () => window.clearTimeout(off);
  }, [flare]);
  useEffect(() => () => stopSettle.current?.(), []);

  const onStop = useCallback(() => void actions.stopRun(), [actions]);

  // Which turns have a parked card filed in them. Passing a `<CardStack/>` to
  // every turn regardless would defeat `Turn`'s own memo — a fresh child
  // element per render is a changed prop — and most turns never hold one.
  const parkedIn = useMemo(() => {
    const keys = new Set<string>();
    for (const p of state.permissions) if (p && p.decision && p.parkedIn) keys.add(p.parkedIn);
    return keys;
  }, [state.permissions]);

  // T:13698 appends a row per failure AND draws the card for the newest one in
  // the same place; here the card lives at the tail of the log, so the newest
  // row would say the same thing twice. Suppress that ONE row.
  const supersededBy = state.trouble ? lastErrorKey(state.turns) : null;

  return (
    <div className="chat-logwrap" ref={port}>
      <div className="chat-log" ref={log}>
        {state.historyLoading ? (
          <HistorySkeleton />
        ) : (
          <>
            {state.turns.map((turn) => {
              // The failure the trouble card at the tail is already reporting is
              // NOT also a row: the row is the record of the failures BEFORE it.
              if (turn.key === supersededBy) return null;
              return (
                <Turn
                  key={turn.key}
                  turn={turn}
                  anchored={turn.role === "user" && !!flare && turn.uuid === flare}
                  {...(tail && tail.turnKey === turn.key ? { tail } : {})}
                  {...(onShowSent ? { onShowSent } : {})}
                >
                  {/* Parked cards belong to the turn they were answered in —
                      whichever turn that was, streaming or long finished.
                      `streaming` is cleared at run end (run-controller's
                      `runEnding`), so gating on it made every resolved card
                      vanish from the transcript the moment its turn ended; T
                      parks the node into the turn's DOM and it stays
                      (T:14728-14742). */}
                  {parkedIn.has(turn.key) ? (
                    <CardStack
                      rows={state.permissions}
                      placement="parked"
                      turnKey={turn.key}
                      liveMode={liveMode}
                      pickerMode={pickerMode}
                      actions={actions}
                    />
                  ) : null}
                </Turn>
              );
            })}
            {state.trouble ? (
              <TroubleView trouble={state.trouble} {...(what ? { what } : {})} />
            ) : null}
            {/* Open cards form one contiguous block ending at the status line. */}
            <CardStack
              rows={state.permissions}
              placement="open"
              liveMode={liveMode}
              pickerMode={pickerMode}
              actions={actions}
            />
            {state.working ? (
              <WorkingLine working={state.working} status={state.status} onStop={onStop} />
            ) : null}
          </>
        )}
      </div>
    </div>
  );
});

/** The open cards' ids, in request order, as one string — the dependency for
 *  "a card the run is blocked on appeared". Exported for the test: the rule is
 *  per card MOUNT, and a count cannot express it (T:14652-14663). */
export function openCardIds(rows: ChatState["permissions"]): string {
  return rows
    .filter((p) => p && p.id && !p.decision)
    .map((p) => p.id)
    .join(",");
}

/** The last `role: "error"` row's key, which is the one the trouble card at the
 *  tail is reporting (the controller writes both in one patch). Exported for the
 *  test: the rule is "the newest error row is the card", not "any error row". */
export function lastErrorKey(turns: ChatState["turns"]): string | null {
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === "error") return turns[i].key;
  return null;
}

/** T:12885-12891 — compared as a string against what the render wrote rather
 *  than built into a selector: a uuid off a url is untrusted input, and
 *  `[data-msg="…"]` with a quote in it throws. */
function findTurn(log: HTMLElement | null, uuid: string): HTMLElement | null {
  if (!log) return null;
  for (const el of log.querySelectorAll<HTMLElement>(".turn[data-msg]"))
    if (el.dataset.msg === uuid) return el;
  return null;
}

function scrollToAnchor(el: HTMLElement, still: boolean): void {
  try {
    el.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "center" });
  } catch {
    el.scrollIntoView(); // an engine that takes no options object
  }
}

/** Hold the anchored turn in view while the transcript finishes settling, and
 *  only while it has actually drifted OUT of view — a turn that is merely
 *  off-centre has already done its job. Any deliberate scroll cancels the rest
 *  outright: once the reader has taken the wheel, nothing here may take it back
 *  (T:12902-12945). */
function settleAnchor(wrap: HTMLElement, el: HTMLElement, still: boolean): () => void {
  const timers: number[] = [];
  const stop = () => {
    for (const t of timers) window.clearTimeout(t);
    timers.length = 0;
    for (const ev of ["wheel", "touchstart", "pointerdown", "keydown"])
      wrap.removeEventListener(ev, stop);
  };
  for (const ev of ["wheel", "touchstart", "pointerdown", "keydown"])
    wrap.addEventListener(ev, stop, { passive: true });
  const last = ANCHOR_SETTLE_MS[ANCHOR_SETTLE_MS.length - 1];
  for (const delay of ANCHOR_SETTLE_MS) {
    timers.push(
      window.setTimeout(() => {
        if (!el.isConnected) return stop();
        // Out of the scroller's box, not merely off its centre.
        const box = wrap.getBoundingClientRect();
        const seen = el.getBoundingClientRect();
        if (seen.bottom <= box.top || seen.top >= box.bottom) scrollToAnchor(el, still);
        if (delay === last) stop();
      }, delay),
    );
  }
  return stop;
}

/** While `history` is in flight. The SHAPE of a conversation, not a grey block:
 *  a right-aligned prompt and a reply behind the avatar, so the column does not
 *  jump when the real turns land. */
function HistorySkeleton() {
  return (
    <div className="chat-skeleton" aria-hidden="true">
      {[0, 1].map((i) => (
        <div key={i} className="chat-skeleton">
          <div className="chat-skeleton-row is-user">
            <Skeleton className="h-8 w-1/2 rounded-2xl" />
          </div>
          <div className="chat-skeleton-row">
            <Skeleton className="size-6 shrink-0 rounded-full" />
            <div className="flex w-full flex-col gap-2">
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-4/5" />
              <Skeleton className="h-3 w-2/3" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default Transcript;
