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
import { cn } from "@platform/lib/utils";

import type { ChatController, ChatState, UserTurn } from "../protocol/controller-api";
import type { PermissionMode, Segment } from "../protocol/types";
import { CardStack, type CardActions } from "./CardStack";
import { TroubleView } from "./TroubleView";
import type { Viewable } from "./attachApi";
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
  /** PR2: a receipt's thumbnail or glyph opens the full-size viewer. */
  onOpenShot?: (shot: Viewable) => void;
  /** "preview" / "app" — the word a receipt's nouns use (`PaneState.paneNoun`). */
  paneNoun?: string;
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
  onOpenShot,
  paneNoun,
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

  // ── THE ANSWERED CARD'S RECEIPT COMES INTO VIEW (T:14738-14742) ───────────
  //
  // Answering a card while scrolled up to re-read the reply moved it out of the
  // bottom stack and into its turn — and left the "✓ Allowed" receipt off
  // screen, so the click had no visible consequence at all. T is careful about
  // the scope: "Following the tail is the log's own rule while a run streams;
  // otherwise the most this may do is keep the card the user was just looking at
  // on screen — scrolling a reader who is somewhere else entirely would be the
  // move yanking the page." Hence both conditions, `!followTail && wasVisible`.
  //
  // `wasVisible` has to be read BEFORE the move, and T can: it does the
  // `appendChild` itself. React re-renders the row in its new place, so by the
  // time a layout effect runs the old box is gone — which is why visibility is
  // sampled for the OPEN cards on every commit and read back on the transition.
  // Cheap: open cards are 0 or 1 in almost every state, and the run is blocked
  // while there is one.
  const cardWasVisible = useRef(new Map<string, boolean>());
  const wereOpen = useRef<string[]>([]);
  useLayoutEffect(() => {
    const wrap = port.current;
    const open = state.permissions
      .filter((p) => p && p.id && (!p.decision || !p.parkedIn))
      .map((p) => p.id);
    // The transition, off the PREVIOUS commit's open set: a row that was open
    // and is now filed into a turn.
    //
    // NOT WHILE A CARD IS OPEN (Bugbot, PR #1074). One poll can both answer a
    // card and open the next one, and the open-card effect above runs FIRST —
    // so this pass would land last and pull the viewport back to the receipt,
    // hiding the card the run is blocked on. An open card is the hard block
    // (T:14652-14663 scrolls to it unconditionally); a receipt is a courtesy.
    // T reaches the same answer by another road: `parkResolvedCard` runs
    // `followBottom()` before this scroll, and with a card open the log is
    // following.
    if (wrap && !followTail.current && !open.length) {
      const parked = new Set(
        state.permissions.filter((p) => p && p.id && p.decision && p.parkedIn).map((p) => p.id),
      );
      for (const id of wereOpen.current) {
        if (!parked.has(id)) continue;
        if (!cardWasVisible.current.get(id)) continue;
        const el = findCard(log.current, id);
        // `block: "nearest"` and nothing else, exactly as T has it: the least
        // the browser can do to make the receipt reachable, rather than
        // centring it and moving a reader who did not ask to be moved.
        if (el) el.scrollIntoView({ block: "nearest" });
      }
    }
    // …and re-sample for the next commit.
    const seen = new Map<string, boolean>();
    if (wrap) {
      const portBox = wrap.getBoundingClientRect();
      for (const id of open) {
        const el = findCard(log.current, id);
        if (!el) continue;
        const box = el.getBoundingClientRect();
        seen.set(id, box.bottom > portBox.top && box.top < portBox.bottom);
      }
    }
    cardWasVisible.current = seen;
    wereOpen.current = open;
  }, [state.permissions, state.rev]);

  // ── ONE SCROLLER, NEVER TWO (R3-4) ───────────────────────────────────────
  //
  // The pin used to be capped at 70% of the scrollport, which bought a second
  // scrollbar on every card taller than that — the transcript's behind the
  // card's, side by side, and the reader's wheel answered by whichever box the
  // pointer happened to be over (owner: "remove the 70% cap — it causes double
  // scroll"). The cap is gone: a pinned card may now be as tall as the whole
  // scrollport, and a card TALLER than that becomes the only thing that scrolls.
  //
  // Which of those two states we are in cannot be asked in CSS, so it is
  // MEASURED — overflow, off the two boxes, never a breakpoint or a fraction.
  // A card that fits leaves the transcript scrollable, because that is what the
  // pin is for: re-reading the reply the card is asking about. A card that does
  // not fit covers the transcript completely, so locking it costs the reader
  // nothing and takes the second scrollbar away.
  const pin = useRef<HTMLDivElement>(null);
  const [pinFull, setPinFull] = useState(false);
  /** Where the transcript was when the lock went on. `overflow: hidden` clamps
   *  `scrollTop` to 0, and handing back the TOP of a conversation the reader was
   *  part-way down is its own bug — so the offset is parked across the lock and
   *  put back when the card is answered. */
  const parkedTop = useRef(0);
  const locked = useRef(false);
  useEffect(() => {
    const box = pin.current;
    const wrap = port.current;
    if (!box || !wrap || !openCards) {
      setPinFull(false);
      locked.current = false;
      return;
    }
    const measure = () => {
      // `+ 1` because both numbers are rounded off fractional layout, and a
      // half-pixel is not an overflow worth locking a scroller for.
      const full = box.scrollHeight > wrap.clientHeight + 1;
      // Read BEFORE the class lands: by the time an effect keyed on `pinFull`
      // runs, the browser has already clamped this to 0.
      if (full && !locked.current) parkedTop.current = wrap.scrollTop;
      locked.current = full;
      setPinFull(full);
    };
    measure();
    // OBSERVED, not measured once: a card grows after it mounts (an "Other"
    // field opening, a diff arriving, a question's options wrapping), which is
    // the same argument the follow-bottom rules make above.
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [openCards]);
  useLayoutEffect(() => {
    const wrap = port.current;
    if (!wrap || pinFull || !parkedTop.current) return;
    wrap.scrollTop = parkedTop.current;
    parkedTop.current = 0;
  }, [pinFull]);

  // ── the first paint lands at the bottom (#26) ────────────────────────────
  //
  // "Opening a session with an open card flashes the top of the chat then
  // scrolls down." Every rule above writes `scrollTop` in a LAYOUT effect, which
  // is before paint — but only for the tree that has already laid out. A
  // restored session paints its turns first (`historyLoading` false is the frame
  // the turns arrive in) and the pictures, highlighted code blocks and expanded
  // chips inside them keep GROWING for several frames after; each of those is
  // answered by the ResizeObserver, and the reader watches the transcript walk
  // down to the tail.
  //
  // So the log is not painted at all until the first of those writes has landed:
  // `is-settling` is `visibility: hidden` (it still lays out — that is the point),
  // and it comes off in the same layout effect that scrolls, so no frame is ever
  // shown at the top. `settled` starts TRUE where there is no layout to wait for
  // (a test renderer, SSR): hiding a tree that can never be measured would hide
  // it for good.
  //
  // AND THE SAME RULE COVERS ADOPTION (R2-11). On the cards wall a tile that is
  // waiting on a question painted its transcript first, scrolled itself to the
  // bottom, and only then — when `adoptLiveRun` had found the run and its first
  // poll had delivered the permission row — grew the card, which moved
  // everything again: two flashes for one open. `state.adopting` is the run
  // controller saying "there may still be a live run to attach to here", so the
  // log stays hidden across that window and the card and the turns arrive in the
  // same frame.
  //
  // READ STRUCTURALLY, not off the type: the flag is the protocol layer's to
  // add and this file must not have to land in the same commit. Absent, it is
  // `undefined` — which is not `true`, so a controller that never publishes it
  // behaves exactly as before.
  const adopting = (state as { adopting?: boolean }).adopting === true;
  const [settled, setSettled] = useState(() => typeof ResizeObserver === "undefined");
  const firstBottom = useRef(false);
  useLayoutEffect(() => {
    if (firstBottom.current || state.historyLoading || adopting) return;
    firstBottom.current = true;
    if (port.current) port.current.scrollTop = port.current.scrollHeight;
    setSettled(true);
  }, [state.historyLoading, adopting, state.rev]);

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

  // WHERE each parked card sits INSIDE its turn (#18). Passing a `<CardStack/>`
  // to every turn regardless would defeat `Turn`'s own memo — a fresh child
  // element per render is a changed prop — and most turns never hold one.
  const parked = useMemo(() => parkPlan(state.turns, state.permissions), [
    state.turns,
    state.permissions,
  ]);

  return (
    <div className={cn("chat-logwrap", pinFull && "is-locked")} ref={port}>
      <div className={cn("chat-log", !settled && "is-settling")} ref={log}>
        {state.historyLoading ? (
          <HistorySkeleton />
        ) : (
          <>
            {state.turns.map((turn) => {
              const plan = parked.get(turn.key);
              // A `<CardStack/>` for one position, by id: the same list, the
              // same three card kinds, restricted to the rows that belong here.
              const stack = (ids: string[]) => (
                <CardStack
                  rows={state.permissions}
                  ids={ids}
                  placement="parked"
                  turnKey={turn.key}
                  liveMode={liveMode}
                  pickerMode={pickerMode}
                  actions={actions}
                />
              );
              const after = plan?.after.size
                ? new Map(Array.from(plan.after, ([i, ids]) => [i, stack(ids)]))
                : null;
              return (
                <Turn
                  key={turn.key}
                  turn={turn}
                  anchored={turn.role === "user" && !!flare && turn.uuid === flare}
                  {...(tail && tail.turnKey === turn.key ? { tail } : {})}
                  {...(onShowSent ? { onShowSent } : {})}
                  {...(after ? { cardsAfter: after } : {})}
                  {...(onOpenShot ? { onOpenShot } : {})}
                  {...(paneNoun ? { paneNoun } : {})}
                >
                  {/* Parked cards belong to the turn they were answered in —
                      whichever turn that was, streaming or long finished.
                      `streaming` is cleared at run end (run-controller's
                      `runEnding`), so gating on it made every resolved card
                      vanish from the transcript the moment its turn ended; T
                      parks the node into the turn's DOM and it stays
                      (T:14728-14742). These are the ones with no tool chip of
                      their own to sit under, so they go at the turn's tail. */}
                  {plan?.tail.length ? stack(plan.tail) : null}
                </Turn>
              );
            })}
            {/* THE ROW AND THE CARD, both (#27). T appends a red row per failure
                AND draws the actionable card for the newest one; the port
                suppressed the row the card duplicated, which left an API error
                or a session limit with no line at the end of the log saying the
                turn had stopped — "last line = the error" is the request. The
                row is the log entry, the card is what the user can act on. */}
            {state.trouble ? (
              <TroubleView trouble={state.trouble} {...(what ? { what } : {})} />
            ) : null}
            {/* THE TAIL PIN (#17). An OPEN card sticks to the bottom of the
                scrollport for as long as it is open, because the run cannot
                continue without it and a reader who has scrolled up to re-read
                the reply cannot otherwise find it. The working line comes with
                it — "Waiting for your approval" and the thing to approve are one
                statement. Answered cards are NOT in here: they have already been
                parked back into their turn above, at the chip they answered. */}
            <div
              className={cn("chat-tailpin", openCards && "is-pinned")}
              ref={pin}
            >
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
            </div>
          </>
        )}
      </div>
    </div>
  );
});

/** One turn's parked cards: which segment each sits AFTER, plus the ones with
 *  nowhere better to go. */
export interface ParkPlan {
  /** segment index → the ids of the cards drawn immediately after it. */
  after: Map<number, string[]>;
  /** Card ids for the turn's tail. */
  tail: string[];
}

/**
 * WHERE A RESOLVED CARD PARKS (#18): immediately after the tool chip it
 * answered, in chronological position, every time.
 *
 * The port filed every parked card at the END of its turn, which is right when
 * the approval was the last thing that happened in the turn and arbitrary the
 * rest of the time — a Bash approved twenty chips ago showed its receipt under
 * the final paragraph, and the same conversation reloaded put it somewhere else
 * again because a later turn had become the live one.
 *
 * The chip is found in two steps, and the second is the one that works today:
 *   1. `row.toolUseId` against the segment's own `tool_use` id — exact, and the
 *      field the protocol layer is being asked to carry (agent.py already writes
 *      `tool_use_id` into the permission request; the poll's row shape drops it);
 *   2. otherwise the LAST not-yet-claimed tool segment of the same tool NAME.
 *      A turn's approvals arrive in the order the tools do, so walking the cards
 *      in arrival order and claiming chips left to right reconstructs the
 *      pairing for the ordinary case (one Bash, one Edit, one Write) and for
 *      repeats of the same tool.
 * A card that matches neither — an AskUserQuestion, a plan, an approval whose
 * chip never made it into the replayed transcript — goes to the turn's tail,
 * which is where it was before.
 */
export function parkPlan(
  turns: ChatState["turns"],
  rows: ChatState["permissions"],
): Map<string, ParkPlan> {
  const out = new Map<string, ParkPlan>();
  const claimed = new Map<string, Set<number>>();
  for (const p of rows) {
    if (!p || !p.id || !p.decision || !p.parkedIn) continue;
    const key = p.parkedIn;
    let plan = out.get(key);
    if (!plan) out.set(key, (plan = { after: new Map(), tail: [] }));
    const turn = turns.find((t) => t.key === key);
    const segs = (turn && turn.role === "assistant" ? turn.segments : null) ?? [];
    let taken = claimed.get(key);
    if (!taken) claimed.set(key, (taken = new Set<number>()));
    const at = chipFor(segs, p, taken);
    if (at < 0) {
      plan.tail.push(p.id);
      continue;
    }
    taken.add(at);
    const list = plan.after.get(at);
    if (list) list.push(p.id);
    else plan.after.set(at, [p.id]);
  }
  return out;
}

/** The index of the tool chip a card answered, or -1. See `parkPlan`. */
function chipFor(
  segs: readonly Segment[],
  row: ChatState["permissions"][number],
  taken: Set<number>,
): number {
  // TO WIRE (protocol): `PermissionRow.toolUseId`, from the permission
  // request's `tool_use_id` — permission_server.py already writes it into the
  // .req.json, agent.py's poll row just does not forward it. Read structurally
  // rather than off the type so this file does not have to land in the same
  // commit as `protocol/types.ts`; the name-match fallback below covers the
  // ordinary case until it arrives.
  const wanted = (row as { toolUseId?: string }).toolUseId;
  if (wanted) {
    for (let i = 0; i < segs.length; i++) {
      const seg = segs[i];
      if (seg.kind === "tool" && seg.id === wanted) return i;
    }
  }
  if (!row.tool) return -1;
  for (let i = segs.length - 1; i >= 0; i--) {
    const seg = segs[i];
    if (seg.kind === "tool" && seg.name === row.tool && !taken.has(i)) return i;
  }
  return -1;
}

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
/** The card node for a permission row. A `dataset` walk rather than an
 *  attribute selector, for the reason `findTurn` below is one: a row id is
 *  server-shaped and would have to be CSS-escaped to be safe in a selector,
 *  and there is never more than a handful of cards to walk. */
function findCard(log: HTMLElement | null, id: string): HTMLElement | null {
  if (!log) return null;
  for (const el of log.querySelectorAll<HTMLElement>("[data-perm-id]"))
    if (el.dataset.permId === id) return el;
  return null;
}

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
