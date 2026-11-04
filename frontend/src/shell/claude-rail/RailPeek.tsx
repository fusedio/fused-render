// THE CARD THAT OPENS OFF THE CLAUDE RAIL (owner, 2026-10-30: "when it is
// collapsed can we show a card preview of it when I hover over the chat
// items; I should be able to interact with it as well — scroll and add
// something, click something"). Behind `canvas_rail_peek_enabled`
// (rail-peek-flag.ts); the rings' side of the wiring is ClaudeRail.tsx, the
// timings and the geometry are rail-peek-lib.ts. A hover on a ring opens it;
// a press holds it open (ClaudeRail header) — the card itself draws the same
// either way.
//
// WHAT IT IS: the chat itself. Not a caption, not a picture — the same
// `ChatMount` the Tasks page's side peek frames (`peek`, `chatOnly`), on the
// hovered chat's session, in a 420×640 card a gap off the rail. The reader
// scrolls the transcript, answers a question, types a message, follows a
// link, as in the pane; "Open" in the card's head is the door the ring's own
// press opens (`taskHref`), for when a preview turns into a session. The
// head is two rows: facts and seats (×, star, number, where, unread + when,
// archive, Open, ⋮), then the title alone.
//
// WHY IT NEVER FLASHES:
//   - ONE CARD, kept in the DOM, shown and hidden by a class: the open is an
//     opacity fade and a 6px slide (var(--dur-med)); the close the reverse.
//     Moving from ring to ring moves the card and swaps what is in it; the
//     card itself never closes and reopens.
//   - PLACED, THEN SHOWN, on two frames: the box is set while the card is
//     hidden (no transition runs on a hidden card's position), and the
//     `is-on` class lands on the next frame — so a card that closed at the
//     bottom of the rail and opens at the top does not fly between the two.
//     With the card up, a position change DOES glide, which is the swap.
//   - `position: fixed` in a body portal, with no transform on the box: a
//     transformed ancestor would re-root every fixed-position popover inside
//     the chat (the model menu, the attach picker). The slide is on an inner
//     wrapper and ends at `transform: none` once the card is up, which is no
//     containing block — the 6px offset exists only while hidden or fading,
//     when the card is inert and nothing in it can open a popover.
//
// WHY IT IS FAST: every previewed chat stays mounted after its card closes
// (`kept`, newest first, PEEK_KEEP of them) — `visibility: hidden`, not
// `display: none`, so its scroll position and layout survive — and the
// hovered one's mount starts on the pointer's arrival, before the open wait
// has run (ClaudeRail `warm`). The chat's chunk is fetched when the rail
// mounts with the flag on (`preloadChat`).
//
// A PRESS ON A RING LANDS IN THE COMPOSER (owner, 2026-10-31: "when we click
// the chat item we focus on the card composer so user can type — instead of
// opening the sidebar"): `focusComposer`, retried until the chat has mounted.
//
// A PINNED CARD RESIZES (owner, 2026-11-02: "when we click to preview let's
// give the panel ability to resize … top left and bottom left corner … and
// we remember that size; only for click though, hover size remain same"):
// two grips on the card's left corners, shown only while the card is PINNED
// (`pinned`: a ring press, a press inside) and the seat has a chat.
// The right edge stands against the rail, so a drag moves the left edge for
// the width; the top-left grip moves the top edge (the bottom stays), the
// bottom-left the bottom (the top stays) — rail-peek-lib `dragResize`, with
// its minimums and the row's edges. The size lands in the ui-layout
// document (`canvasRailPeek`, D1386: it must survive a relaunch on another
// port) and is the size of every pinned card from then on; a hover card is
// always the default 420×640, so a hover keeps reading as a glance and a
// click as the thing the reader shaped. The drag moves the box literally
// (a top-left drag lifts the top edge); when it ENDS the card is placed
// again at its new size — top level with the ring, as always (owner,
// 2026-11-02: "when resize ends, we should match top to top again"). A
// press INSIDE a hover card pins
// it too — but the remembered size waits for that press to END (`grown`):
// a card that grew under a pressed pin would move the pin out from under
// the pointer before the click landed.
//
// × AND STAR (owner, 2026-11-03: "remove the pin; a click keeps the card open
// anyway — keep a close, and a star beside it to favourite the chat"): the
// head's first two seats are the × — always there, it is the one way to shut a
// card a click has held, bar the Open door — and the STAR, which makes the
// chat a favourite (`setTaskFavourite`; the server keeps the stars, so every
// rail and pane lists the same ones). Starred, the star is filled in the
// accent. A favourite's card is the one that may stay open between visits:
// the server records which favourite is open (`starred_open`), so the ×
// on a starred card writes "closed" too, and unstarring the favourite's own
// card shuts it (`dismissOnUnstar`: the server drops its record, the ring
// leaves the foot). The rail's card never shows a starred chat: a star
// lets it go (`onStarred`) and the list leaves the chat out (ClaudeRail
// header, 2026-11-04). The rail reopens the recorded one on its own
// (ClaudeRail header).
//
// ACCESSIBILITY: the card is a non-modal `dialog` named for its chat; the ring
// that opened it says `aria-expanded` and `aria-controls` (ClaudeRail). The
// keyboard's way in is ArrowLeft on a focused ring (`focusChat` lands in the
// composer, or on the card's first button while the chat is still loading);
// Escape anywhere in the card hands focus back to the ring and closes. A card
// that holds focus never closes under the pointer (PeekIntent `hold`).
import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal } from "react-dom";
import { Archive, ArchiveRestore, MessageSquare, Pencil, Star, X } from "lucide-react";
import { notify } from "@platform/lib/notifications";
import { archiveTask, renameTask, setTaskFavourite, unarchiveTask, type Task } from "@platform/lib/api";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { pokeTasks } from "@shell/tasksPulse";
import { useArchiveConfirm } from "@shell/archive-confirm";
import { shortTaskId } from "@platform/lib/task-id";
import { ChatMount, draftHref } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import { AgentProvider } from "@apps/claude/ui/agent-context";
import { timeAgo } from "@platform/lib/format";
import { chatDraftKey, peekDraftSyncer } from "@platform/lib/drafts";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import {
  cardTitleLine,
  isDraftTask,
  messageStamp,
  nextRunAt,
  recapTitlesOn,
  relativeWhen,
  taskColumn,
  taskHref,
  taskWhen,
  type CardTitle,
} from "@shell/tasks-lib";
import { nodeName, railScope, type RailPlace, type RailRow } from "./claude-rail-lib";
import {
  PEEK_MAX_HEIGHT,
  PEEK_MAX_WIDTH,
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  PEEK_PLAIN_HEIGHT,
  PEEK_WIDTH,
  STAR_MARK_MS,
  dragResize,
  errorWords,
  inSafeCone,
  placeClear,
  starMarkStep,
  starOutcome,
  statusWords,
  type Box,
  type PeekWish,
  type StarMark,
} from "./rail-peek-lib";

/** The size the reader last dragged a pinned card to (ui-layout
 *  `canvasRailPeek`), floored at the grips' minimums; null for "never". */
function storedPeekSize(): { width: number; height: number } | null {
  const s = getUiLayout().canvasRailPeek;
  if (!s) return null;
  return {
    width: Math.min(PEEK_MAX_WIDTH, Math.max(PEEK_MIN_WIDTH, s.width)),
    height: Math.max(PEEK_MIN_HEIGHT, s.height),
  };
}

type Corner = "tl" | "bl";

/** The pin glyph (owner, 2026-11-02: "BsFillPinAngleFill use this") —
 *  Bootstrap Icons' `pin-angle-fill` (MIT), inlined: react-icons is not a
 *  dependency and one path does not earn one. Filled, angled, 16-unit box.
 *  The head's first seat while favourites are OFF (favourites-flag). */
function PinAngleFill({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a6 6 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707s.512-1.22.707-1.414l3.182-3.182-2.828-2.829a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a6 6 0 0 1 1.013.16l3.134-3.133a3 3 0 0 1-.04-.461c0-.43.108-1.022.589-1.503a.5.5 0 0 1 .353-.146" />
    </svg>
  );
}

/** The server's cap on a task's name (TaskPeekWho's). */
const TITLE_MAX_LENGTH = 200;

/** What one seat is, for the head and the body: the pulse row, the full row
 *  once held, and the answers both halves ask (RailPeek `seatFacts`). */
interface SeatFacts {
  t: RailRow["pulse"];
  row: Task | null;
  /** The pulse's live status over the full row (or the pulse alone). */
  merged: Task | RailRow["pulse"];
  label: string;
  /** Where its press goes: a draft's New task card, else the chat's door. */
  href: string | null;
  draft: boolean;
  /** `#T12` — "" on a draft, which has no task number yet. */
  id: string;
  scope: ReturnType<typeof railScope>;
  words: string;
  /** How many messages the chat holds (the listing's `message_count`; 0
   *  until the full row is held, or for a chat with none). */
  count: number;
  /** Replies not yet read, 0 for none. */
  unread: number;
  /** When the chat last moved, in the list's relative register ("3m ago"); "" when it never has. */
  when: string;
  /** …and the same moment in full, for the hover. */
  whenFull: string;
  /** The server can rename it: a started session (TaskPeekWho `renamable`). */
  renamable: boolean;
  /** The title line as the List row builds it (its `hint` is the whole text). */
  line: CardTitle;
  /** The reply line the List row prints after the title in italics — only
   *  with task recap titles OFF (with them on, the title IS the recap); ""
   *  when the row holds none, or is pulse-only. */
  reply: string;
}

function seatFacts(seat: RailRow, place: RailPlace): SeatFacts {
  const { pulse: t, row } = seat;
  const merged = row ? { ...row, status: t.status } : t;
  // The List's own title line (recap as the title once recaps are on).
  const line = cardTitleLine(merged);
  const draft = !!row && isDraftTask(row);
  const href = (row && draft ? draftHref(row) : null) ?? taskHref(merged);
  return {
    t,
    row,
    merged,
    label: line.text || t.title || "Chat",
    href,
    draft,
    id: draft ? "" : shortTaskId(t.task_id),
    scope: railScope(t, place),
    words: draft ? "draft" : statusWords(t, !!row?.failed),
    count: Math.max(0, row?.message_count ?? 0),
    unread: t.unread > 0 ? t.unread : 0,
    // The List row's own clock and caption (tasks-lib `taskWhen`: "Last run
    // <time>", "Drafted <time>"…) once the full row is held; the pulse's
    // own stamp until then.
    when: row ? taskWhen(merged as Task).text : relativeWhen(t.happened_at || t.last_active || 0),
    whenFull: row ? taskWhen(merged as Task).title : messageStamp(t.happened_at || t.last_active || 0),
    renamable: !!t.session_id && !draft && !/^(pending|draft|new):/.test(t.key),
    line,
    reply: row && !recapTitlesOn(row) ? (row.last_reply || "").trim() : "",
  };
}

/** THE ROW THE RAIL STANDS IN — the box the card is placed against and
 *  watched for resizes: the rail's nearest ancestor that HAS a box. The
 *  explorer's file view portals the rail into `.stat-side-slot`, which is
 *  `display: contents` (explorer.css) and so measures as nothing; the row
 *  there is the split behind it. */
function rowOf(rail: HTMLElement | null): HTMLElement | null {
  let el = rail?.parentElement ?? null;
  while (el && getComputedStyle(el).display === "contents") el = el.parentElement;
  return el;
}

export interface RailPeekHandle {
  /** Put the keyboard in the shown chat's composer (or on the card's first
   *  button while the chat is still loading). False when nothing is shown. */
  focusChat(): boolean;
  /** Put the keyboard in the shown chat's composer and nowhere else: false
   *  until the composer has mounted (the chat chunk may still be loading), so
   *  a press that wants to type is never parked on the card's × meanwhile. */
  focusComposer(): boolean;
}

export interface RailPeekProps {
  /** The card's DOM id — what the ring's `aria-controls` names. */
  id: string;
  /** The seat the card shows, or null while it is closed (the last shown
   *  seat stays drawn through the fade-out). */
  shown: RailRow | null;
  /** Every seat whose chat stays mounted: the shown one and the recently
   *  previewed ones (rail-peek-lib `keepAlive`). */
  kept: readonly RailRow[];
  /** The ring element for a seat (its anchor), or null if it is not drawn. */
  anchorOf: (key: string) => HTMLElement | null;
  /** The rail, for its edge and for the row it stands in (its parent). */
  railEl: HTMLElement | null;
  /** Where the rail stands (claude-rail-lib `RailPlace`): what a row's
   *  target is read against to name its node or file (`railScope`), and —
   *  on a canvas — the selected node, whose chip is a label, not a door. */
  place: RailPlace;
  /** The ⋮ filed or deleted a task: re-read the listing now rather than on
   *  the pulse's next move. */
  onReload: () => void;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  /** The pointer entered (`true`) or left (`false`) the safe cone between
   *  the shown ring and the card, or the bridge over the gap (rail-peek-lib
   *  `inSafeCone`, PeekIntent `enterCone`/`leaveCone`). */
  onCone: (inside: boolean) => void;
  /** A pointer went down inside the card — on the card itself or on one of
   *  the chat's portaled popovers (React's capture phase crosses portals,
   *  the DOM's `contains` does not): hold it through the gesture, and tell
   *  the host's outside-press watcher this press was ours. */
  onPressInside: () => void;
  /** The card took (`true`) or lost (`false`) the keyboard focus. */
  onHold: (on: boolean) => void;
  /** Escape inside the card, or its close button — or the Open door
   *  (`"door"`), or an unstar that shuts the card (`"unstar"`,
   *  `dismissOnUnstar`): a host that records "closed" on the × keeps the
   *  record through the door, and writes nothing for an unstar, which
   *  dropped the record already (FavouritePeek). */
  onDismiss: (why?: "door" | "unstar") => void;
  /** The row left no room for a card (rail-peek-lib `peekPlacement` null —
   *  a window under ~140px tall or ~170px wide): the host drops the show so
   *  the ring is not left saying `aria-expanded` for a card that never drew. */
  onNoRoom: () => void;
  /** "Open in panel →", and any hop the chat itself makes. */
  onOpenChat: (href: string) => void;
  /** The head's node chip: select that node in the workbench (its name, as
   *  the workbench names it — the file's stem). Canvas pages only; without
   *  it the chip is a label. */
  onSelectNode?: (udfName: string) => void;
  /** The chip was pressed: pin the shown card (a keyboard press has no
   *  pointer-down to do it, `onPressInside`), and see the keyboard into its
   *  composer once that has mounted (ClaudeRail `focusComposerSoon`). */
  onPin: () => void;
  /** The card is HELD (owner, 2026-11-03: a press on a ring or inside the
   *  card keeps it open): nothing but its × — or the Open door — closes it:
   *  not an outside press, not a press into the workbench, not Escape. */
  held: boolean;
  /** The × / Open / an unstar let go (`false`). */
  onHeld: (held: boolean) => void;
  /** The star was pressed and the server answered: `on` is the star as it
   *  now stands. Ahead of the pulse, for the rail's own bookkeeping. */
  onStarred?: (key: string, on: boolean) => void;
  /** A FIXED SIZE AND EDGE (the favourite's card, shell/claude-rail
   *  FavouritePeek): the card takes this wish for every seat instead of the
   *  default / stored sizes, and never grows on a pin. */
  size?: PeekWish;
  /** Another card's box to keep clear of (the favourite's card, at the
   *  rail's foot; rail-peek-lib `placeClear`: above it, else to its left,
   *  else over it as the last resort). */
  avoid?: Box | null;
  /** An UNSTAR shuts this card (the favourite's own card: its record is
   *  gone). The rail's card leaves it false — it lets go on the star
   *  itself (`onStarred`), so an unstar never reaches it. */
  dismissOnUnstar?: boolean;
  /** Said in the body instead of the chat while the shown seat's chat is not
   *  mounted here (`kept` leaves it out — FavouritePeek, while the pane or
   *  the Open door shows the same session: one chat, one composer). */
  bodyNote?: string | null;
  /** Where the card stands now, or null while it is closed — for a host
   *  that places another card around it. */
  onBox?: (box: Box | null) => void;
  /** The card is PINNED (PeekIntent: a ring press, a press inside) — not a
   *  passing hover. Only a pinned card wears the corner grips
   *  and takes the remembered size (header). */
  pinned: boolean;
}

/** A seat with no conversation yet (header of the body above). */
function PlainBody({ facts }: { facts: SeatFacts }) {
  const { row, draft, label } = facts;
  const form = (row?.form ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  // The words: a task draft's description, a chat draft's composer preview.
  // Never the title again — it is in the head.
  const preview = str(row?.draft?.preview);
  const description = str(form.description);
  const words = (draft ? description || preview : "") || "";
  const same = words.toLowerCase() === label.trim().toLowerCase();
  const saved = draft ? timeAgo(row?.draft?.updated_at) : null;
  // The run itself, not the List's chip: `nextRunChip` is null for an
  // Upcoming row because the row's own stamp already says the time there —
  // this card has no such stamp (CodeRabbit on #2029).
  const at = !draft && row ? nextRunAt(row) : null;
  const next = at ? `Runs ${relativeWhen(at)} · ${messageStamp(at)}` : null;
  const meta = draft
    ? [str(form.model), str(form.effort), str(form.permission)].filter(Boolean)
    : [str(row?.model), str(row?.effort)].filter(Boolean);
  return (
    <div className="claude-rail-peek-plain">
      <div className="claude-rail-peek-plain-row">
        <span className={"claude-rail-peek-pill" + (draft ? " is-draft" : "")}>
          {draft ? "Draft" : "Scheduled"}
        </span>
        <span className="claude-rail-peek-plain-when">
          {draft
            ? saved
              ? `Not sent · saved ${saved}`
              : "Not sent"
            : next ?? "Not started yet"}
        </span>
      </div>
      <p className={"claude-rail-peek-plain-words" + (!words || same ? " is-empty" : "")}>
        {words && !same ? words : draft ? "No description yet." : "No message yet."}
      </p>
      {meta.length > 0 && <p className="claude-rail-peek-plain-meta">{meta.join(" · ")}</p>}
    </div>
  );
}

export const RailPeek = forwardRef<RailPeekHandle, RailPeekProps>(function RailPeek(
  {
    id,
    shown,
    kept,
    anchorOf,
    railEl,
    place: where,
    onReload,
    onPointerEnter,
    onPointerLeave,
    onCone,
    onPressInside,
    onHold,
    onDismiss,
    onNoRoom,
    onOpenChat,
    onSelectNode,
    onPin,
    held,
    onHeld,
    onStarred,
    size,
    avoid = null,
    dismissOnUnstar = false,
    bodyNote = null,
    onBox,
    pinned,
  },
  ref,
) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  // The last seat shown, so the fade-out has something to fade — the head
  // AND the chat: both key on `view`, so the transcript stays visible through
  // the fade rather than vanishing under a fading empty frame (Bugbot).
  const last = useRef<RailRow | null>(null);
  if (shown) last.current = shown;
  const view = shown ?? last.current;
  const viewKey = view?.pulse.key ?? null;

  const [box, setBox] = useState<Box | null>(null);
  const [on, setOn] = useState(false);
  const shownKey = shown?.pulse.key ?? null;

  // One composer ref per kept mount (ChatMount `focusRef`).
  const focusRefs = useRef(new Map<string, MutableRefObject<HTMLTextAreaElement | null>>());
  const focusRefFor = (key: string) => {
    let r = focusRefs.current.get(key);
    if (!r) {
      r = { current: null };
      focusRefs.current.set(key, r);
    }
    return r;
  };
  for (const key of [...focusRefs.current.keys()]) {
    if (!kept.some((s) => s.pulse.key === key)) focusRefs.current.delete(key);
  }

  // WHAT SIZE (header): a seat with no session gets the short card
  // (PEEK_PLAIN_HEIGHT); a pinned chat card takes the remembered size, and
  // keeps a top-left drag's top while it stays on the same chat
  // (`dragTop`); a hover card is the default. Refs, not state: `place` runs
  // from listeners and effects whose closures would otherwise go stale.
  // GROWN (header): the pinned card takes the remembered size — at once for
  // a ring press, after the pointer comes up for a press inside
  // the card (`onPressInside` runs in the capture phase, before the pin
  // flips; the window's pointerup ends it).
  const [grown, setGrown] = useState(false);
  const grownRef = useRef(false);
  grownRef.current = grown;
  const pressingInside = useRef(false);
  const growAfterPress = useRef(false);
  const onPressInsideCapture = () => {
    pressingInside.current = true;
    onPressInside();
  };
  useEffect(() => {
    const up = () => {
      pressingInside.current = false;
      if (growAfterPress.current) {
        growAfterPress.current = false;
        setGrown(true);
      }
    };
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", up, true);
    return () => {
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", up, true);
    };
  }, []);
  useEffect(() => {
    if (!pinned) {
      growAfterPress.current = false;
      setGrown(false);
    } else if (pressingInside.current) growAfterPress.current = true;
    else setGrown(true);
  }, [pinned]);
  const wishFor = (key: string): PeekWish => {
    if (size) return size;
    if (!kept.find((s) => s.pulse.key === key)?.pulse.session_id) return { maxHeight: PEEK_PLAIN_HEIGHT };
    const stored = grownRef.current ? storedPeekSize() : null;
    if (!stored) return { maxHeight: PEEK_MAX_HEIGHT };
    return { width: stored.width, maxHeight: stored.height };
  };
  // The rail's left edge as of the last placement: where the bridge below
  // ends. A ref, not state — `place` runs before every `setBox`, so the
  // render that draws the new box reads a fresh value.
  const railLeft = useRef(0);
  // The node the canvas page has selected (its file), if any (RailPeekProps
  // `place`).
  const selectedFile = where.kind === "canvas" ? where.file ?? null : null;
  // Resolved once per rail element, not per placement: `rowOf` reads computed
  // style, and `place` runs on every scroll of the rail's list.
  const row = useMemo(() => rowOf(railEl), [railEl]);
  const place = (key: string): Box | null => {
    const anchor = anchorOf(key);
    if (!anchor || !railEl || !row) return null;
    const rail = railEl.getBoundingClientRect();
    railLeft.current = rail.left;
    // Clear of the other card (`avoid`): above it, else beside it, else —
    // a window too small for either — over it (rail-peek-lib `placeClear`).
    return placeClear(anchor.getBoundingClientRect(), rail, row.getBoundingClientRect(), avoid, wishFor(key));
  };
  // THE CORNER DRAG (header). `resizing` names the held grip while a drag is
  // on: the card drops its glide transitions and the placement effects stand
  // back so the box follows the pointer alone.
  const [resizing, setResizing] = useState<Corner | null>(null);
  const resizingRef = useRef<Corner | null>(null);
  resizingRef.current = resizing;
  const dragStart = useRef<{ x: number; y: number; box: Box; pointerId: number; corner: Corner; el: HTMLElement } | null>(null);
  // The box the last move asked for — what the release saves. A ref, not
  // `box`: the move's state write may not have committed when the pointer
  // comes up (reviewer).
  const dragLast = useRef<Box | null>(null);
  const onGripDown = (corner: Corner) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStart.current = { x: e.clientX, y: e.clientY, box, pointerId: e.pointerId, corner, el: e.currentTarget };
    dragLast.current = null;
    setResizing(corner);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId || !row) return;
    const next = dragResize(start.corner, start.box, e.clientX - start.x, e.clientY - start.y, row.getBoundingClientRect());
    dragLast.current = next;
    setBox((prev) =>
      prev && prev.left === next.left && prev.top === next.top && prev.width === next.width && prev.height === next.height
        ? prev
        : next,
    );
  };
  // The drag ends: `save` on the grip's own release; not when the card swapped
  // chat, unpinned or hid under the pointer (the effect below) — that drag
  // was about a card that is gone (CodeRabbit on #2062). Either way the
  // placement effects take over again and set the card level with its ring
  // at the size it now has (header).
  const endDrag = (save: boolean) => {
    const start = dragStart.current;
    dragStart.current = null;
    setResizing(null);
    const last = dragLast.current;
    dragLast.current = null;
    if (start && start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    // A press that never moved saves nothing — not the default size, not a
    // size the row clamped (reviewer).
    if (!save || !start || !last || !shownKey || (last.width === start.box.width && last.height === start.box.height)) return;
    // WHAT IS REMEMBERED (Bugbot and CodeRabbit on #2062): only a dimension
    // the drag changed, and only when the row let it reach the minimum — a
    // height-only drag in a narrow window must not persist the width the row
    // clamped the card to, and a drag in a row shorter than PEEK_MIN_HEIGHT
    // must not replace a remembered height with the row's. The other
    // dimension keeps what was remembered, else the pinned card's default.
    const prev = storedPeekSize();
    const pick = (changed: boolean, got: number, min: number, kept: number | undefined, fallback: number) =>
      changed && got >= min ? got : (kept ?? fallback);
    const width = pick(last.width !== start.box.width, last.width, PEEK_MIN_WIDTH, prev?.width, PEEK_WIDTH);
    const height = pick(last.height !== start.box.height, last.height, PEEK_MIN_HEIGHT, prev?.height, PEEK_MAX_HEIGHT);
    // SET, never toggle (ui-layout header): the same answer on any base.
    patchUiLayout((d) =>
      d.canvasRailPeek?.width === width && d.canvasRailPeek?.height === height ? d : { ...d, canvasRailPeek: { width, height } },
    );
  };
  const onGripUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    endDrag(true);
  };
  // The grip can leave mid-drag (Escape, the chat pruned, the card unpinned
  // or hidden), or the card can SWAP chat under it (another ring's press):
  // the drag is cancelled there, unsaved — or `.is-resizing` would stick,
  // the follow-the-ring paths would stand back for good (reviewer), and a
  // release would save a size that was about the old chat (CodeRabbit).
  const visible = on && box !== null && shownKey !== null;
  // The host that places another card around this one hears where it is.
  const onBoxRef = useRef(onBox);
  onBoxRef.current = onBox;
  useEffect(() => {
    onBoxRef.current?.(visible ? box : null);
  }, [visible, box]);
  useEffect(() => () => onBoxRef.current?.(null), []);
  const gripsOn = pinned && !size && visible && !!view?.pulse.session_id;
  const dragKey = useRef<string | null>(null);
  useEffect(() => {
    if (dragStart.current && (!gripsOn || dragKey.current !== shownKey)) endDrag(false);
    dragKey.current = shownKey;
    // `endDrag` reads refs; the flag and the key are what decide.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gripsOn, shownKey]);
  // A ui-layout hydrate that lands after the first render replaces the
  // cache without a render of ours: re-render, so a pinned card up at that
  // moment is re-placed at the saved size (CodeRabbit on #2062).
  const [, setLayoutTick] = useState(0);
  useEffect(() => subscribeUiLayout(() => setLayoutTick((n) => n + 1)), []);

  // PLACED, THEN SHOWN (header). Hidden → shown sets the box on this frame and
  // the class on the next; shown → shown (a swap) moves the box under the
  // class, which glides; shown → hidden drops the class and keeps the box.
  useLayoutEffect(() => {
    if (!shownKey) {
      setOn(false);
      return;
    }
    const next = place(shownKey);
    if (!next) {
      onNoRoom();
      return;
    }
    setBox(next);
    const id = requestAnimationFrame(() => setOn(true));
    return () => cancelAnimationFrame(id);
    // `place` reads the DOM; the key is what decides when to re-read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  // A WARM MOUNT NEEDS A REAL BOX: the first chat mounts before anything is
  // shown, and a chat laid out at 0×0 would do its first paint and its
  // scroll-to-end in a box it is about to leave. Place the hidden card for
  // the warmed ring, so the mount lays out at the size it will be shown at.
  const warmKey = kept[0]?.pulse.key ?? null;
  useLayoutEffect(() => {
    if (box || !warmKey) return;
    setBox(place(warmKey) ?? { left: -PEEK_WIDTH - 50, top: 0, width: PEEK_WIDTH, height: PEEK_MAX_HEIGHT });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [box, warmKey]);

  // FOLLOW THE RING while up: a window resize, the rail's own list scrolling
  // under the pointer on a short window, and the row changing shape (the
  // sidebar folding, a pane opening); plus every render, cheaply — the pulse
  // re-sorts the rail, and a ring that moved takes its card with it.
  useEffect(() => {
    if (!shownKey || !railEl) return;
    const re = () => {
      if (resizingRef.current) return;
      const next = place(shownKey);
      // The row shrank under the card (a window resized down to nothing):
      // the card leaves rather than standing clamped over the canvas.
      if (!next) {
        onNoRoom();
        return;
      }
      setBox((prev) =>
        next && prev && next.left === prev.left && next.top === prev.top && next.width === prev.width && next.height === prev.height
          ? prev
          : next,
      );
    };
    window.addEventListener("resize", re);
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, railEl]);
  useEffect(() => {
    if (!shownKey || resizing) return;
    const next = place(shownKey);
    if (next && box && (next.top !== box.top || next.left !== box.left || next.width !== box.width || next.height !== box.height))
      setBox(next);
  });

  // LEAVING A CHAT SAVES ITS DRAFT (owner, 2026-10-30: "if I make the draft
  // in the card it doesn't update instantly"). A session chat's composer
  // writes its unsent words on window blur, page hide or its own UNMOUNT —
  // never per keystroke (Composer, "one rule for every composer") — and a
  // kept-alive mount never unmounts: the card hid, the words sat unwritten,
  // the ring's draft dot waited for the pane to open. So the card hiding, or
  // swapping to another chat, is the leave: the chat that just left view
  // flushes its draft syncer now, and the listing feed carries the dot back
  // to the ring (ClaudeRail `onDraftChange` → `refreshListing`).
  const lastShownKey = useRef<string | null>(null);
  useEffect(() => {
    const prev = lastShownKey.current;
    lastShownKey.current = shownKey;
    if (!prev || prev === shownKey) return;
    const seat = kept.find((s) => s.pulse.key === prev);
    const sid = seat?.pulse.session_id;
    if (!sid) return;
    const file = seat.row?.target || seat.pulse.target || seat.pulse.project;
    peekDraftSyncer(chatDraftKey(sid, file))?.flushNow();
    // `kept` only matters for the seat that just left; the key is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  // THE SAFE CONE (header; rail-peek-lib `inSafeCone`): while the card is up,
  // every pointer move is tested against the triangle from the shown ring's
  // centre to the card's padded right edge. Inside → `onCone(true)`: the
  // card holds and a ring crossed on the way does not swap; outside →
  // `onCone(false)`: the intent re-arms for whatever the pointer rests on.
  // The bridge's own enter/leave feed the same switch, so one clock
  // (PEEK_CONE_MS) covers both.
  const coneOn = useRef(false);
  const setCone = (inside: boolean) => {
    if (coneOn.current === inside) return;
    coneOn.current = inside;
    onCone(inside);
  };
  useEffect(() => {
    if (!on || !box || !shownKey) return;
    const anchor = anchorOf(shownKey);
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const apex = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    const onMove = (e: PointerEvent) => {
      const inCard = cardRef.current?.contains(e.target as Node) ?? false;
      setCone(!inCard && inSafeCone({ x: e.clientX, y: e.clientY }, apex, box));
    };
    document.addEventListener("pointermove", onMove, true);
    return () => {
      document.removeEventListener("pointermove", onMove, true);
      setCone(false);
    };
    // `onCone` is the host's stable callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on, box, shownKey]);

  // INERT WHILE HIDDEN (React 18 has no `inert` prop): the fading card and
  // the warm mounts behind it take no focus, no hit, and are out of the
  // accessibility tree — `aria-hidden` on a subtree that might still hold
  // focus is the thing this avoids.
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);

  useImperativeHandle(
    ref,
    () => ({
      focusChat() {
        if (!shownKey) return false;
        const box = focusRefs.current.get(shownKey)?.current;
        const target = box ?? cardRef.current?.querySelector<HTMLElement>("button");
        if (!target) return false;
        target.focus({ preventScroll: true });
        return true;
      },
      focusComposer() {
        const box = shownKey ? focusRefs.current.get(shownKey)?.current : null;
        if (!box) return false;
        box.focus({ preventScroll: true });
        return true;
      },
    }),
    [shownKey],
  );

  // WHICH FOCUS HOLDS THE CARD. Typing (an editable), a popup of the chat's
  // (its ⋮ menu, a picker, a dialog), or focus that the KEYBOARD brought here
  // (Tab, an arrow — the ArrowLeft way in) — those are a reader using the
  // card, and it must not close under them. A plain button focused by a
  // click, or by a menu handing focus back to its trigger on Escape, is not:
  // a reader who closed the ⋮ and moved the pointer away expects the card to
  // follow the pointer rule again, not to stand until a second Escape.
  const lastKeyAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" || e.key.startsWith("Arrow")) lastKeyAt.current = Date.now();
    };
    // A pointer press focuses by mouse: a click right after a Tab is not
    // keyboard focus (CodeRabbit on #2029).
    const onPointer = () => {
      lastKeyAt.current = 0;
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, []);
  const onFocusCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    const editable =
      t.matches("textarea, input, select, [contenteditable=''], [contenteditable='true']");
    // NOT the card itself, which is a `dialog` too: the nearest popup role
    // above the target has to be something INSIDE the card.
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    const popup = !!pop && pop !== cardRef.current;
    const keyboard = Date.now() - lastKeyAt.current < 400;
    onHold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    onHold(false);
  };
  // Escape ON THE HEAD only. The chat hands its own Escape up through
  // `onEscape` once nothing of its own (a menu, the shot viewer) is open; a
  // card-wide handler would close the card under an open menu and, having
  // prevented default, stop the chat's handler from closing the menu itself.
  const onHeadKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape" && !e.defaultPrevented && !heldRef.current) {
      e.preventDefault();
      onDismiss();
    }
  };

  const head = view ? seatFacts(view, where) : null;

  // THE ⋮ IS THE CHAT'S OWN (owner, 2026-10-30: "it is not the same kebab
  // menu — fix that"): `@apps/claude/ui/Kebab`, the menu the pane's chat
  // wears — continue in terminal, copy the command, download the transcript,
  // archive, delete — with its own labels, refusals and erase dialog. Its
  // popup is a portal; the card HOLDS through it because the popup takes the
  // keyboard focus and React's focus events cross portals (`onFocusCapture`
  // → `onHold(true)`), and a press on one of its rows is "inside" by the
  // same route (`onPressInside`). Filing or erasing the task re-reads the
  // listing; the pulse then takes the ring, and the card with it (prune).
  const onFiled = () => {
    // A filed or erased chat leaves the rail: a held card lets go with it
    // (a hold must not keep a ring for a chat the reader just put away).
    onHeld(false);
    onReload();
  };
  // The Archive seat's call can finish after a ring press swapped the card
  // to another (held) chat: only the chat that was filed lets go of the hold,
  // read off the ref when the call returns, not when it started.
  const shownKeyRef = useRef(shownKey);
  shownKeyRef.current = shownKey;
  const onFiledKey = (key: string) => {
    if (shownKeyRef.current === key) onHeld(false);
    onReload();
  };

  // THE ARCHIVE SEAT (owner, 2026-11-02): the ⋮'s Archive as a button of its
  // own on the head's first row. Same door, same rule: a run in flight asks
  // first (`useArchiveConfirm`, the wording every single-task archive
  // shares), a filed task re-reads the listing and the pulse takes the ring.
  // Its dialog renders INSIDE the card so a press on it is "inside"
  // (`onPressInside` runs in React's capture phase, which crosses portals)
  // and the card holds through the ask. `filing` greys the seat while the
  // call is out, so a second press cannot file the task twice.
  const archiveAsk = useArchiveConfirm();
  const cancelArchiveAsk = archiveAsk.cancel;
  // Which chat the pending ask is about: the dialog draws only while that
  // chat is still the shown one — a swap or close with the ask up must not
  // leave a Confirm that files the chat that left view.
  const askedKey = useRef<string | null>(null);
  const [filing, setFiling] = useState(false);
  const fileTask = (facts: SeatFacts) => {
    askedKey.current = facts.t.key;
    const archived = facts.t.status === "archived";
    const go = async () => {
      setFiling(true);
      try {
        if (archived) await unarchiveTask(facts.t.key);
        else await archiveTask(facts.t.key);
        onFiledKey(facts.t.key);
      } catch (e) {
        notify({ title: (e as Error).message, tone: "error" });
      } finally {
        setFiling(false);
      }
    };
    if (archived) void go();
    else
      archiveAsk.guard(
        // The pulse's status and jobs, not the listing row's: the row can still
        // read "done" a moment after a new turn started.
        { status: facts.t.status, kind: facts.row?.kind ?? "task", background_jobs: facts.t.background_jobs },
        () => void go(),
      );
  };

  // THE STAR (header): the server stores it, the pulse brings it back to
  // every rail. A plain star (`setTaskFavourite` with `open` omitted) opens
  // the favourite's card only when no other favourite is open — else it is
  // recorded closed. UNSTARRING shuts the card only under `dismissOnUnstar`
  // (the favourite's card host, FavouritePeek) — its ring leaves the foot,
  // and a card with no ring to stand beside lets go — and only if the card
  // is still on that chat when the call returns.
  // THE STAR SHOWS THE ANSWER AT ONCE (`starMark`): the pulse that carries
  // `starred` back is a poll away — seconds on a busy Mac — and a star that
  // stays hollow after its press reads as a dead button. The server's answer
  // is kept per key until the pulse agrees — or has had its chance to: the
  // SECOND distinct pulse row after the press, or STAR_MARK_MS, ends it
  // whatever it says (another window may have flipped the star meanwhile;
  // rail-peek-lib `starMarkStep`). The rail is told too (`onStarred`), so its
  // own rules (letting go of a chat the favourite's card takes) run before
  // the pulse.
  const favsOn = useTaskFavourites();
  const [starMark, setStarMark] = useState<StarMark | null>(null);
  const starSeq = useRef(0);
  const starOf = (t: RailRow["pulse"]): boolean =>
    starMark && starMark.key === t.key ? starMark.on : !!t.starred;
  useEffect(() => {
    if (!starMark) return;
    // The shown seat too: a card drawn without its chat (`bodyNote`) keeps
    // nothing in `kept`.
    const t = (kept.find((s) => s.pulse.key === starMark.key) ?? (view?.pulse.key === starMark.key ? view : null))?.pulse;
    const next = starMarkStep(starMark, t);
    if (next !== starMark) setStarMark(next);
  }, [kept, view, starMark]);
  const starMarkSeq = starMark?.seq ?? 0;
  useEffect(() => {
    if (!starMarkSeq) return;
    const id = window.setTimeout(() => setStarMark((m) => (m && m.seq === starMarkSeq ? null : m)), STAR_MARK_MS);
    return () => window.clearTimeout(id);
  }, [starMarkSeq]);
  const toggleStar = async (t: RailRow["pulse"]) => {
    const on = !starOf(t);
    let res: Awaited<ReturnType<typeof setTaskFavourite>>;
    try {
      res = await setTaskFavourite(t.key, on);
    } catch (e) {
      notify({ title: errorWords(e, "The star could not be saved"), tone: "error" });
      return;
    }
    starSeq.current += 1;
    setStarMark({ key: t.key, on: res.starred, pulse: t, seen: 0, seq: starSeq.current });
    onStarred?.(t.key, res.starred);
    if (starOutcome(res.starred, t.key, shownKeyRef.current, dismissOnUnstar).dismiss) {
      onHeld(false);
      onDismiss("unstar");
    }
    onReload();
    pokeTasks();
  };

  // A HELD card ignores Escape (header): the head's and the chat's Escape
  // both read the flag off a ref, so the chat's handler need not re-mount.
  const heldRef = useRef(held);
  heldRef.current = held;
  const onChatEscape = useCallback(() => {
    if (!heldRef.current) onDismiss();
  }, [onDismiss]);

  // THE TITLE IS THE READER'S TO RENAME (owner, 2026-11-02: "on title hover
  // we can bring edit icon, we can name our tasks") — the side peek's own
  // flow (TaskPeekWho): the pencil that shows on hover, or a double-click,
  // turns the title into an input; Enter or blur saves, Escape cancels,
  // saving what is stored does nothing, and saving "" gives the automatic
  // title back. The input's Escape is its own (`defaultPrevented`), so the
  // head's Escape does not close the card under it.
  const [draft, setDraft] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState("");
  const renameSettled = useRef(false);
  const startRename = (facts: SeatFacts) => {
    if (!facts.renamable || renaming) return;
    renameSettled.current = false;
    setRenameError("");
    setDraft(facts.label.slice(0, TITLE_MAX_LENGTH));
  };
  const cancelRename = () => {
    renameSettled.current = true;
    setDraft(null);
    setRenameError("");
  };
  const saveRename = async (facts: SeatFacts) => {
    if (draft === null || renameSettled.current || renaming) return;
    const next = draft.slice(0, TITLE_MAX_LENGTH).trim();
    // Against the STORED name, not the shown line (a recap or last-message
    // title is not the task's name; saving it unchanged is how the reader
    // pins it). Only a renamed task has a name to match.
    const stored = facts.merged.title_source === "renamed" ? (facts.merged.title || "").split("\n")[0].trim() : "";
    if (next === stored) return cancelRename();
    // Settled NOW, before the await: Enter's save and the blur the disabled
    // input then fires must not send the rename twice (reviewer).
    renameSettled.current = true;
    setRenaming(true);
    try {
      await renameTask(facts.t.key, next);
      setDraft(null);
      setRenameError("");
      pokeTasks();
      onReload();
    } catch (e) {
      renameSettled.current = false;
      setRenameError((e as Error).message || "Could not rename");
      // The disabled input lost the keyboard; give it back so Enter retries.
      requestAnimationFrame(() => titleInputRef.current?.focus());
    } finally {
      setRenaming(false);
    }
  };
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  // IS THE TITLE CLIPPED? Measured, not guessed: the span's scroll width past
  // its box, re-read when the line or the card's width changes (the card
  // narrows on a narrow window, rail-peek-lib `peekPlacement`). Decides
  // whether the title wears the app's hint (below).
  const titleRef = useRef<HTMLSpanElement | null>(null);
  const [titleClipped, setTitleClipped] = useState(false);
  const titleText = view ? seatFacts(view, where).label : "";
  useLayoutEffect(() => {
    const el = titleRef.current;
    if (!el) {
      setTitleClipped(false);
      return;
    }
    const check = () => setTitleClipped(el.scrollWidth > el.clientWidth + 1);
    check();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [titleText, draft, viewKey]);
  // A swap to another chat, or the card closing, drops an open rename: the
  // input would otherwise save into the chat that left view, or come back
  // half-typed when the card reopens on the same chat.
  useEffect(() => {
    renameSettled.current = true;
    setDraft(null);
    setRenameError("");
    // The same goes for an Archive ask still waiting for its answer: it was
    // about the chat that left view and must not come back with it.
    askedKey.current = null;
    cancelArchiveAsk();
  }, [shownKey, cancelArchiveAsk]);

  // THE BRIDGE (owner, 2026-10-30: "an invisible [strip] between the item I
  // am hovering and the preview, so the user doesn't have to move fast or
  // time it"): a transparent band over the gap between the card's right edge
  // and the rail, the card's full height, that counts as the card for the
  // pointer — resting in the gap holds the card open past the grace, and
  // the canvas wrapper under the gap never sees the pointer (it focuses its
  // frame on enter). It stops 3px short of the rail so the rail's seam (the
  // drag that opens the pane) keeps its own hit area. Only while the card is
  // up (`.is-on`), like the card itself.
  const bridge =
    box && railLeft.current > box.left + box.width + 1
      ? { left: box.left + box.width, top: box.top, width: Math.max(0, railLeft.current - 3 - (box.left + box.width)), height: box.height }
      : null;

  return createPortal(
    <>
    {bridge && (
      <div
        className={"claude-rail-peek-bridge" + (visible ? " is-on" : "")}
        aria-hidden="true"
        style={bridge}
        onPointerEnter={() => setCone(true)}
        onPointerLeave={() => setCone(false)}
      />
    )}
    <div
      ref={cardRef}
      id={id}
      className={
        "claude-rail-peek" +
        (visible ? " is-on" : "") +
        (held && !favsOn ? " is-held" : "") +
        (resizing ? " is-resizing" : "") +
        (view && !view.pulse.session_id ? " is-plain" : "")
      }
      role="dialog"
      aria-label={head ? `${head.label} — preview` : "Chat preview"}
      style={
        box
          ? view?.pulse.session_id
            ? { left: box.left, top: box.top, width: box.width, height: box.height }
            : { left: box.left, top: box.top, width: box.width }
          : undefined
      }
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onPointerDownCapture={onPressInsideCapture}
      onFocusCapture={onFocusCapture}
      onBlurCapture={onBlurCapture}
    >
      {gripsOn &&
        // THE GRIPS (header): the card's two left corners, hit areas with
        // a resize cursor, only on a pinned chat card. `aria-hidden`: a
        // pointer-only affordance; the card's size is a preference, not a
        // control the keyboard needs.
        (["tl", "bl"] as const).map((corner) => (
          <div
            key={corner}
            className={"claude-rail-peek-grip is-" + corner + (resizing === corner ? " is-active" : "")}
            aria-hidden="true"
            onPointerDown={onGripDown(corner)}
            onPointerMove={onGripMove}
            onPointerUp={onGripUp}
            onPointerCancel={onGripUp}
            onLostPointerCapture={() => dragStart.current && endDrag(true)}
          />
        ))}
      <div className="claude-rail-peek-card">
        {head && (
          // THE HEAD, TWO ROWS (owner, 2026-11-02, after the one-row head of
          // 2026-10-30): the first row is the task's FACTS and SEATS — the ×
          // and the star (2026-11-03), its number where the
          // Tasks page prints it, WHERE it lives (the canvas, or one node —
          // the pane's chats do not say), then at the right its unread
          // count and when it last moved (the archive shows over that on
          // hover, the List row's way), the door as a word, and the ⋮. The
          // second row is the TITLE alone, full width, with the pencil on
          // hover: a recap title is a sentence, and a sentence squeezed
          // between chips on one row lost its end.
          <div className="claude-rail-peek-head" onKeyDown={onHeadKeyDown}>
            <div className="claude-rail-peek-row">
              {!favsOn ? (
                // FAVOURITES OFF (favourites-flag): the head of 2026-11-02,
                // unchanged — the pin, which pressed IS the ×.
                <button
                  type="button"
                  className={"claude-rail-peek-btn claude-rail-peek-pin" + (held ? " is-held" : "")}
                  aria-pressed={held}
                  title={held ? "Unpin and close the preview" : "Pin the preview: it stays until you unpin it"}
                  aria-label="Pin the preview"
                  onClick={() => {
                    if (held) {
                      onHeld(false);
                      onDismiss();
                    } else {
                      onHeld(true);
                      onPin();
                    }
                  }}
                >
                  {held ? <X size={14} aria-hidden="true" /> : <PinAngleFill size={14} />}
                </button>
              ) : (
              <button
                type="button"
                className="claude-rail-peek-btn claude-rail-peek-close"
                aria-label="Close the preview"
                title="Close the preview"
                onClick={() => {
                  // A favourite's open card: the × is the reader closing it,
                  // and ClaudeRail writes "closed" as the card leaves (its
                  // shown-key rule) — the same way for every door out.
                  onHeld(false);
                  onDismiss();
                }}
              >
                <X size={14} aria-hidden="true" />
              </button>
              )}
              {favsOn && head.t.session_id && (
              // Behind the favourites switch (favourites-flag). Only a
              // STARTED chat can be starred (a draft or a queued row has no
              // key that lasts; the server refuses them too).
              <button
                type="button"
                className={"claude-rail-peek-btn claude-rail-peek-star" + (starOf(head.t) ? " is-on" : "")}
                aria-pressed={starOf(head.t)}
                // One label, the state in `aria-pressed`.
                aria-label="Favourite"
                title={starOf(head.t) ? "Remove from favourites" : "Add to favourites"}
                onClick={() => void toggleStar(head.t)}
              >
                <Star size={14} fill={starOf(head.t) ? "currentColor" : "none"} aria-hidden="true" />
              </button>
              )}
              {head.id && (
                <span className="claude-rail-peek-id" title={head.words}>
                  {head.id}
                </span>
              )}
              {head.scope.kind === "node" && onSelectNode ? (
                // The node's chip is a door: it selects the node in the
                // workbench (which zooms to it and shows its code). The card
                // STAYS (owner, 2026-10-31: "when I click on the tag, shouldn't
                // the chat preview still stay open") — the press is inside the
                // card, so it pins it like any other; the rail then narrows to
                // that node, whose rings still hold this chat, and the card
                // follows its ring. The selected node's chip is the SAME
                // button, `aria-disabled` (still focusable): swapping it for a
                // span when the selection echo lands would unmount the focus
                // it may hold (Bugbot on #2044). Names, not files: the page's
                // selected file is a guess from the echo (claude-rail-lib
                // `onRail`).
                (() => {
                  const current = !!selectedFile && nodeName(selectedFile) === head.scope.name;
                  return (
                    <button
                      type="button"
                      className="claude-rail-peek-scope is-node"
                      aria-disabled={current || undefined}
                      title={
                        current
                          ? `On the node ${head.scope.name} (${head.scope.file}), selected`
                          : `Select the node ${head.scope.name} (${head.scope.file}) in the workbench`
                      }
                      onClick={(e) => {
                        // Pinned FIRST: the selection may reshape the rail (it
                        // narrows to the node), and only a pinned card is held
                        // through that (CodeRabbit on #2044).
                        onPin();
                        if (!current && head.scope.kind === "node") onSelectNode(head.scope.name);
                        // Back to the composer: the reader is still in this chat,
                        // and WebKit leaves a pressed button unfocused — focus on
                        // the body, where a later press into the canvas frame is
                        // not heard as the window's blur and so never closes the
                        // pinned card (ClaudeRail's frame-press rule). A seat with
                        // no composer (a draft on the node), or a chat still
                        // mounting, keeps it on the chip — the latter until
                        // `onPin`'s wait moves it on.
                        const box = shownKey ? focusRefs.current.get(shownKey)?.current : null;
                        (box ?? e.currentTarget).focus({ preventScroll: true });
                      }}
                    >
                      {head.scope.name}
                    </button>
                  );
                })()
              ) : head.scope.kind === "whole" ? (
                <span className="claude-rail-peek-scope" title={`On the ${head.scope.word.toLowerCase()} as a whole`}>
                  {head.scope.word}
                </span>
              ) : (
                // A FILE under the folder (the app page, the explorer's folder
                // view), or a node with no workbench to select it in: where the
                // chat is, as a label — the full path in the tooltip.
                <span className="claude-rail-peek-scope is-file" title={`On ${head.scope.file}`}>
                  {head.scope.name}
                </span>
              )}
              <span className="claude-rail-peek-gap" aria-hidden="true" />
              {head.t.session_id && !head.draft && (
                // THE PULSE AND THE ARCHIVE share one seat: the unread count
                // (none → no count, the glyph goes too) and when the chat
                // last moved, muted words; the archive button sits over
                // their right end and shows on hover or focus (owner,
                // 2026-11-02: "only on hover over that area, like the card
                // view"). A chat that never moved has no words — then the
                // archive stands alone, always shown (`is-bare`).
                <span className={"claude-rail-peek-pulse" + (head.when || head.count > 0 ? "" : " is-bare")}>
                  {(head.count > 0 || head.when) && (
                    <span className="claude-rail-peek-meta">
                      {head.count > 0 && (
                        // HOW MANY MESSAGES (owner, 2026-11-02: "number of
                        // messages left of time"), the bubble after the
                        // number as the mockup draws it; unread ones turn the
                        // count to ink, and the hover says how many.
                        <span
                          className={"claude-rail-peek-count" + (head.unread > 0 ? " is-unread" : "")}
                          // The List row's own caption (ScheduleTaskViews
                          // `.tasks-row-msgs`), in the app's hint panel.
                          // `role="img"` so the label is read: a bare span's
                          // aria-label is not announced.
                          role="img"
                          aria-label={
                            `${head.count} message${head.count === 1 ? "" : "s"}` +
                            (head.unread > 0 ? `, ${head.unread} unread` : "")
                          }
                          data-hint={
                            `${head.count} message${head.count === 1 ? "" : "s"} in this task` +
                            (head.unread > 0 ? ` — ${head.unread} unread` : "")
                          }
                        >
                          {head.count}
                          <MessageSquare size={12} aria-hidden="true" />
                        </span>
                      )}
                      {head.when && (
                        // …and the List row's time cell: the exact moment, and
                        // which moment it is ("Last run …", "Drafted …").
                        <span className="claude-rail-peek-when" data-hint={head.whenFull || undefined}>
                          {head.when}
                        </span>
                      )}
                    </span>
                  )}
                  <button
                    type="button"
                    className="claude-rail-peek-btn claude-rail-peek-archive"
                    disabled={filing}
                    // ONE WORD, the List seat's caption (tasks-lib `filingIntent`;
                    // Akshil, 2026-10-05: "just say archive").
                    data-hint={head.t.status === "archived" ? "Unarchive" : "Archive"}
                    aria-label={head.t.status === "archived" ? "Unarchive this task" : "Archive this task"}
                    onClick={() => fileTask(head)}
                  >
                    {head.t.status === "archived" ? (
                      <ArchiveRestore size={14} aria-hidden="true" />
                    ) : (
                      <Archive size={14} aria-hidden="true" />
                    )}
                  </button>
                </span>
              )}
              {head.href && (
                <button
                  type="button"
                  className="btn claude-rail-peek-open"
                  title={head.draft ? "Open the draft" : "Open this chat in the Claude panel"}
                  onClick={() => {
                    onOpenChat(head.href!);
                    // The door closes the card too — a held one as well: a
                    // hop that keeps the rail mounted (a node switch) must
                    // not leave it up.
                    onHeld(false);
                    onDismiss("door");
                  }}
                >
                  Open
                </button>
              )}
              {head.t.session_id && shownKey && (
                // Mounted only while a seat is SHOWN (not through the fade): the
                // menu listens for task announcements and re-reads the listing
                // on each, which a hidden card has no business doing. Inside the
                // chat's AgentProvider, so its terminal hand-off names the
                // chat's own agent, not the default one.
                <span className="claude-rail-peek-kebab">
                  <AgentProvider backend={head.row?.backend ?? head.t.backend ?? ""}>
                    <Kebab
                      // Keyed per chat: the menu caches its labels per session.
                      key={head.t.key}
                      file={head.row?.target || head.t.target || head.t.project}
                      sessionId={head.t.session_id}
                      running={taskColumn(head.t) === "in_progress" || taskColumn(head.t) === "queued"}
                      asking={taskColumn(head.t) === "needs_attention"}
                      queued={taskColumn(head.t) === "queued"}
                      onErased={onFiled}
                      onArchived={onFiled}
                    />
                  </AgentProvider>
                </span>
              )}
            </div>
            <div className="claude-rail-peek-titlerow">
              {draft === null ? (
                <>
                  <span
                    ref={titleRef}
                    className="claude-rail-peek-title"
                    // The List row's own caption (ScheduleTaskViews `.tasks-title`,
                    // hints.ts `renderTaskHint`): the title line and the reply
                    // line WHOLE, in the row's styles. Only when something is
                    // clipped — the title past its box, or a reply beside it
                    // (owner, 2026-11-02: "if truncated and I hover it should
                    // show me the full name in tooltip like we do at other
                    // places"); a title fully in view says nothing twice.
                    {...(titleClipped || head.reply
                      ? {
                          "data-hint": head.line.hint ?? head.label,
                          "data-hint-title": head.line.hint ?? head.label,
                          "data-hint-reply": head.reply,
                        }
                      : {})}
                    onDoubleClick={head.renamable ? () => startRename(head) : undefined}
                  >
                    {head.label}
                  </span>
                  {head.reply && (
                    // THE REPLY LINE: what the List prints after the title in
                    // italics — the last turn's reply, with task recap titles
                    // off (on, the title IS the recap — owner, 2026-11-02:
                    // "show recap … like the Tasks view list"). Same hint as
                    // the title: both lines whole.
                    <span
                      className="claude-rail-peek-reply"
                      data-hint={head.reply}
                      data-hint-title={head.line.hint ?? head.label}
                      data-hint-reply={head.reply}
                    >
                      {head.reply}
                    </span>
                  )}
                  {head.renamable && (
                    <button
                      type="button"
                      className="claude-rail-peek-btn claude-rail-peek-rename"
                      title="Rename task"
                      aria-label="Rename task"
                      onClick={() => startRename(head)}
                    >
                      <Pencil size={12} aria-hidden="true" />
                    </button>
                  )}
                </>
              ) : (
                <>
                  <input
                    ref={titleInputRef}
                    className="claude-rail-peek-title-input"
                    value={draft}
                    maxLength={TITLE_MAX_LENGTH}
                    disabled={renaming}
                    autoFocus
                    aria-label="Task title"
                    aria-invalid={renameError ? true : undefined}
                    title={renameError || undefined}
                    onFocus={(e) => e.currentTarget?.select?.()}
                    onChange={(e) => setDraft(e.target.value)}
                    onBlur={() => void saveRename(head)}
                    onKeyDown={(e) => {
                      // The Enter/Escape that settles an IME candidate is not
                      // ours (keyCode 229 is Safari's spelling of isComposing).
                      if (e.nativeEvent?.isComposing || e.keyCode === 229) return;
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void saveRename(head);
                      } else if (e.key === "Escape") {
                        e.preventDefault();
                        cancelRename();
                      }
                    }}
                  />
                  {renameError && <span className="claude-rail-peek-rename-error">{renameError}</span>}
                </>
              )}
            </div>
            {askedKey.current === head.t.key && archiveAsk.node}
          </div>
        )}
        <div className="claude-rail-peek-body">
          {bodyNote && view?.pulse.session_id && !kept.some((s) => s.pulse.key === viewKey) && (
            <p className="claude-rail-peek-note">{bodyNote}</p>
          )}
          {kept.map((seat) => {
            const { pulse: t, row } = seat;
            const facts = seatFacts(seat, where);
            return (
              <div
                key={t.key}
                className={"claude-rail-peek-chat" + (t.key === viewKey ? " is-shown" : "")}
              >
                {t.session_id ? (
                  <ChatMount
                    file={row?.target || t.target || t.project}
                    sessionId={t.session_id}
                    // The task's own settings, a seed (TaskPeek passes the same).
                    {...(row?.model ? { model: row.model } : {})}
                    {...(row?.effort ? { effort: row.effort } : {})}
                    {...(row?.backend ?? t.backend ? { backend: (row?.backend ?? t.backend)! } : {})}
                    chatOnly
                    peek
                    // A hover must never take the keyboard: the reader is pointing,
                    // not typing, until they click into the card or ArrowLeft in.
                    noFocus
                    // No annotate seats: the card has no canvas of its own to mark.
                    noAnnTools
                    paramsSource="memory"
                    focusRef={focusRefFor(t.key)}
                    onEscape={onChatEscape}
                    // A hop out of the chat (a Recent row, a link) is the ring's
                    // own navigation: the canvas page's hop reader does the rest.
                    onNavigate={onOpenChat}
                  />
                ) : (
                  // NO SESSION YET — a draft, or a scheduled task that has not
                  // run: no conversation to show, so the card is SHORT and
                  // says what the row is in the List's own vocabulary — the
                  // `Draft` pill, when it was last touched, the words it holds
                  // (the form's description or the composer's preview; the
                  // title is already in the head), and the settings it would
                  // run with. The head's Open is its one door.
                  <PlainBody facts={facts} />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
    </>,
    document.body,
  );
});
