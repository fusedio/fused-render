// THE CLAUDE RAIL: what stands at a page's right edge while its Claude pane
// is shut (owner, 2026-10-26: "a very min width column, like we have for the
// fused render left sidebar, just a single width column with the button";
// "rail stays on only when claude panel is collapsed"). Born on the canvas
// page; since 2026-11-01 the app page and the explorer's file and folder
// views draw the same rail (owner: "replicate that same behaviour in explorer
// and app view — exact same behaviour"). WHERE it stands is a `RailPlace`
// (claude-rail-lib): a canvas, a folder, a file — which decides whose chats
// ride it and what the + makes a task on; everything below is the same on
// each. The canvas-specific parts (a node narrows the rail; the foot's canvas
// seat and the card's node chip select in the workbench) are the `canvas`
// place's alone and are said so.
//
// The same 48px column the global sidebar collapses to, in the same vocabulary
// (sidebar.css: .sidebar-rail-btn, .sidebar-rail-items, .sidebar-rail-scroll),
// so the two edges of the window read as one idea. Top to bottom:
//   - the panel glyph (platform/ui/PanelIcon) — the way back IN. Its other
//     half, the Hide, is the first seat of the chat's own top row while the
//     pane is up (ClaudeChat `onHidePane`): exactly one of the two is on
//     screen at any moment, the explorer sidebar's own rule (SideChrome.tsx).
//   - NEW TASK, at the head of the list (owner, 2026-10-28: "the + icon
//     should be at the top of the list and it should show a new task modal
//     when we click it, instead of opening the sidebar"). It opens the Tasks
//     page's own card (shell/NewJobModal) over the canvas, prefilled and
//     locked on the clone the way the project page's Tasks tab opens it —
//     the pane stays shut, and the task lands on the rail through the pulse
//     like any other.
//   - EVERY LIVE CHAT ON THIS CANVAS, in the List view's order (owner,
//     2026-10-27: "show all chats we have here, and when we hover we see
//     recap, title — whatever we show in list view"; 2026-10-28: no archived
//     rows, the List's sort — claude-rail-lib.ts). One status ring each — the
//     Tasks page's own (`StatusIcon`: lane colour, unread fill, "!" when it is
//     waiting on you) — and the hover is the list row's own caption: the same
//     `data-hint-title` / `data-hint-reply` the row's title carries
//     (platform/lib/hints), built from the same `cardTitleLine`. A press opens
//     that chat where the Tasks page's door would (`taskHref`): the canvas
//     chat on its session, or the node selected with its chat on its session.
//     Scrolls on a short window.
//
// A SELECTED NODE NARROWS THE RAIL the way it narrows the open pane (owner,
// 2026-10-29: "collapsed sidebar should behave exact same way as expanded
// sidebar but in a collapsed view"): the chats are that node's alone
// (claude-rail-lib `onRail`), and the + makes a task ON the node — the card
// locked on the node's file, the target the node pane's own chats carry, so
// the new task is filed under the node (`task.canvas.file`) and shows in the
// node's Recent list once the pane opens.
//
// THE + WAITS WHERE THE PANE WAITS: the open pane has no chat to start one
// from until the clone exists ("Loading editor…") or while the selected
// node's file is still being written ("Saving <file>…"), so the + is held
// for the same two waits. A task made before the node's file is on disk
// would carry no pane record and miss the node's Recent list.
//
// A HOVER OPENS THE CHAT ITSELF, behind `canvas_rail_peek_enabled` (owner,
// 2026-10-30): with the flag on, resting on a ring — or focusing it — opens
// that chat as a live card beside the rail (RailPeek.tsx): the transcript to
// scroll, the composer to type into, every seat the pane's chat has. The
// timings (open after 150ms, swap between rings without closing, 250ms of
// grace to cross to the card, never close under a card that holds the
// keyboard) are rail-peek-lib's `PeekIntent`; this file only reports the
// rings' pointer and focus to it and draws what it says. A HOVER-ONLY card
// goes with the pointer (on the grace); a card a click has opened is held
// (next paragraph). ArrowLeft on a focused ring is the keyboard's way into the
// card. Off, the rings keep their one-line pointer caption (`data-hint`); on,
// the card replaces it — two captions for one hover would be noise.
//
// A CLICK HOLDS IT (owner, 2026-10-31: "when I hover on an item we show the
// preview; if I click, the preview stays"; 2026-11-03: "a click keeps it open
// — remove the pin button"). A press on a ring (PeekIntent `press`) — or
// anywhere in the card (`pressStart`) — PINS the card, and a pinned card is
// HELD, the one thing: the pointer leaving no longer closes it, and neither
// does a press outside (the +, the panel button and the seam included), a
// press into the workbench frame, Escape, or the all-chats seat — only the
// card's × and its Open do. (Until 2026-11-03 the hold was a second step, a
// pin button in the card's head; the press does both now, `pin` below.) A
// press on a ring never closes it. The ring's press no longer opens the pane;
// the card's Open does, and a modified press still follows the link. THE PRESS LANDS IN THE
// COMPOSER: a plain press puts the keyboard in the card's composer
// (`focusComposerSoon`), ready to type. With no room for a card, the press
// opens the pane as before (the first such press included).
//
// FAVOURITES (owner, 2026-11-03): the card's STAR (RailPeek) makes a chat a
// favourite — server-side, global, so every rail and every pane lists the same
// ones (api `setTaskFavourite`; `starred` on the pulse). Which favourite's card
// is OPEN is per window (2026-11-04, below). The consequences here:
//   - THE RAIL'S LAST SECTION HOLDS THEM: under the Git seats, a ring for
//     every starred chat from ANY place (claude-rail-lib `favSeats` — not
//     `onRail`), newest first. ONLY there: a starred chat on this place
//     leaves the list above (owner, 2026-11-04: "when I fav one task, let's
//     not show it in the list above, let's not have duplicate entries";
//     claude-rail-lib `listSeats`), and is back in it once unstarred. The
//     node's narrowing is the list's alone — the section holds every
//     favourite, node or no node. A held card whose chat is starred lets go
//     (`onStarred`, below) and its ring is the section's from then on.
//   - A FAVOURITE'S RING HOVERS LIKE A LIST RING (owner, 2026-11-04: "when we
//     hover over favourites we don't show preview if there is no favourite
//     open — why?"): resting on it opens THIS rail's hover card on that chat
//     (kept clear of the favourite's card, `avoid`) — unless that favourite's
//     own card is open in this window, where the hover is its caption alone.
//     Its press opens the favourite's card and lets the hover card go.
//   - ONE ACTIVE RING PER SECTION (owner, 2026-11-04: "in one section at a
//     time only one item should be active"): the favourites' is their open
//     card's ring, else their last-pressed ring; the list's is the ring its
//     pinned card shows, else its last-pressed ring, else the URL's session
//     (claude-rail-lib `activeRing`). A hover's wash is a hover, not a mark.
//   - TWO LAST MARKS (owner, 2026-11-04: "last select of fav is diff from
//     last selected of the list, both of them should show their last
//     selected"): the list's `.is-last` and the section's are two stores
//     (platform/lib/last-chat `useLastChat` / `useLastFav`); a press in one
//     moves only its own, and both show at once.
//   - ONE FAVOURITE'S CARD MAY BE OPEN AT A TIME, PER WINDOW (owner,
//     2026-11-04: "if I have multiple windows open, the open favourite
//     shouldn't sync; the list items (stars) do sync"): this window's open
//     key is client-side (platform/lib/last-chat `useFavOpen`), not the
//     server's `starred_open`. That card is the SHELL'S (FavouritePeek), not
//     this rail's: it stands at the bottom right beside whichever rail is on
//     the page, so a page hop never remounts it. A press on a favourite's ring
//     opens it (`openFavourite`), the card's × closes it — nothing is written
//     to the server; this rail only registers itself for it (fav-peek-store)
//     and keeps its own card clear of it (`avoid`: above it, else to its left).
//   - ALL OF IT BEHIND `task_favourites_enabled` (favourites-flag), the ×
//     and the sticky click included: off, the rail is the rail of 2026-11-02
//     — the pin, and an outside press closing a clicked card.
//   - NO ACCENT on a favourite's ring or card — the held accent of 2026-11-02
//     is gone for every card, held ones included.
//
// A PINNED CARD MOVES ONLY ON A PRESS (owner, 2026-11-02: "click only" — the
// click-or-hover trigger pref of 2026-10-31 is gone): resting on another
// ring shows that ring's title and moves nothing; its press swaps the chat.
// A pinned card can also be RESIZED from its left corners, and keeps that
// size for the next pin (RailPeek; a hover card is always the default size).
//
// A press into the workbench frame is not seen by this document; it is heard
// as the window's blur with the frame holding the focus — which is why the
// page stops handing the frame focus on a mere pointer ENTER while a pinned
// card is up (`onCardPinned`, CanvasWorkspace `focusCanvasOnEnter`):
// otherwise crossing the canvas would read as a click on it. (A held card
// ignores that blur too; the rule stays for the moment a hold is let go while
// the pin is still up — a chat filed from its card.)
//
// A PRESS CLEARS THE DOT (owner, 2026-11-01: "if I click on the item it
// should unread and go away"): a plain press on a ring — whether it opens
// the card or the pane — is the reader opening that chat, and opening a
// thread is what clears its badge everywhere else (ScheduleTaskViews
// `openBorrowed`, task-open-read.ts). The write is the List's whole-task
// read; the ring goes hollow at once and stays so until the pulse confirms
// it (claude-rail-lib `readMark` / `keepMarks`: a refused write, or news
// that lands after the press, brings the dot back). A modified press (a new
// tab) clears nothing: the page it opens does its own reading.
//
// THE LAST SEAT WHILE A NODE IS SELECTED is the way back to the whole
// canvas (owner, 2026-11-01: "at the end of that list, only in the collapsed
// list, a canvas icon button which when clicked shows all canvas chats, not
// just filtered chats"): the canvas glyph, which asks the workbench to
// DESELECT (`onSelectNode(null)`) — the selection echo sets `node` back to
// null, and the rail widens to every chat on the canvas, the + with it. It
// stands even when the node has no chat of its own: an empty list with no
// way out of the filter is the thing to avoid.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type FocusEvent as ReactFocusEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { GitBranch, GitCommitHorizontal, Plus, RefreshCw } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { getSchedule, markWholeTaskRead, type Task, type TaskPulseTask } from "@platform/lib/api";
import {
  onDraftChange,
  readListing,
  refreshListing,
  subscribeListing,
  useTasksPulseRows,
} from "@shell/tasksPulse";
import {
  cardTitleLine,
  draftRing,
  isChatDraftTask,
  isDraftTask,
  liveJobRow,
  recapTitlesOn,
  taskColumn,
  taskHref,
} from "@shell/tasks-lib";
import { StatusIcon } from "@shell/ScheduleTaskViews";
import NewJobModal, { chatHopSeed, reopenTime, type DraftSeed } from "@shell/NewJobModal";
import { fetchDrafts } from "@platform/lib/drafts";
import {
  favOpenedAtMs,
  lastChatId,
  setFavOpen,
  setLastChat,
  setLastFav,
  useFavOpen,
  useLastChat,
  useLastFav,
} from "@platform/lib/last-chat";
import { draftHref, preloadChat } from "@apps/claude";
import { hideHint } from "@platform/lib/hints";
import {
  activeRing,
  dotCleared,
  favSeats,
  keepMarks,
  listSeats,
  nodeName,
  placeName,
  placeRoot,
  railRows,
  railScope,
  readMark,
  ringMarks,
  type RailHere,
  type RailPlace,
  type RailRow,
  type ReadMark,
} from "./claude-rail-lib";
import { CanvasGlyph } from "@apps/canvases/glyph";
import { FAV_SETTLE_MS, PeekIntent, keepAlive, railOnStarred, railOpenFav, statusWords } from "./rail-peek-lib";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { focusRail, registerRail, requestFavFocus, takeQuietFocus, useFavPeekState } from "./fav-peek-store";
import { FAV_PEEK_ID } from "./FavouritePeek";
import { chatDraftKey, peekDraftSyncer } from "@platform/lib/drafts";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { useRailGit } from "./rail-git";
import { timeAgo } from "@platform/lib/format";
import { RailPeek, type RailPeekHandle } from "./RailPeek";

/** The hover card's DOM id (one rail per page; the ring's `aria-controls`). */
const PEEK_ID = "claude-rail-peek";

/** The rail's width (claude-rail.css `.claude-rail`): what a shut pane leaves
 *  at the edge, and what a pull to reopen it is measured past (the `closedWidth`
 *  of panel-drag `reopenWidth`). The global sidebar's rail is the same 48px
 *  (platform/lib/sidebarstate SIDEBAR_RAIL_WIDTH). */
export const RAIL_W = 48;

/** How long a press's read may stay in flight before the rail gives the dot
 *  back (CodeRabbit on #2049): a write that hangs would otherwise hold the
 *  ring hollow — and a re-press mute — for as long as the browser waits. The
 *  request itself is not cancelled; a write the server applies after the
 *  deadline shows as the pulse's next zero. */
const READ_WAIT_MS = 8000;

/** How long a press waits for the card's composer to mount before it gives up
 *  putting the keyboard there (a cold chat chunk takes ~1s). */
const FOCUS_WAIT_MS = 2500;

/** What the New task card needs from the schedule before it can open: the
 *  permission modes the server offers and the folders existing tasks point at
 *  (NewJobModal `permissionModes`, `recentTargets` — what Scheduled.tsx hands
 *  it off the schedule it already holds; the rail has no schedule, so it asks
 *  on the press). `seq` keys the mount: every press is a fresh card. */
interface Card {
  seq: number;
  /** The locked target as it was at the press: a node switched while the
   *  schedule read is out must not move the task (Bugbot on #2015). */
  target: string | null;
  /** The card's WHERE pill at the same press (`NewTaskSeed.where`). */
  where: NewTaskSeed["where"];
  modes: string[];
  recents: string[];
  /** A DRAFT REOPENED IN PLACE (owner, 2026-11-02: "draft open took user to
   *  tasks page instead of just opening the modal"): the stored record the
   *  card mounts on — the Tasks page's own seed (`chatHopSeed` for a chat
   *  draft, the row's `form` for a task draft). Absent on the +. */
  draft?: DraftSeed | null;
  /** The chat record a chat draft's card autosaves back onto (NewJobModal
   *  `chatKey`); "" for a task draft. */
  chatKey?: string;
  /** The time the stored form chose, or null (Scheduled `reopenTime`). */
  time?: Date | null;
}

/** What the +'s New task card is about, from the page: the page knows what
 *  the pane's own composer would make a task on, and the rail restates it. */
export interface NewTaskSeed {
  /** The locked target (`NewJobModal lockTarget`): the clone or the node's
   *  file on a canvas, the folder on the app page, the folder or the file in
   *  the explorer. Null while the page cannot say yet. */
  target: string | null;
  /** Why the + is held, or null: the open pane has no chat to start one from
   *  until the clone exists ("Loading the canvas…") or while the selected
   *  node's file is still being written ("Saving <file>…"), so the + waits
   *  for the same two waits. */
  waiting: string | null;
  /** The thing the task is ON, for the +'s words ("New task on hex"); null =
   *  the place as a whole ("New task"). */
  on: string | null;
  /** SAID as the canvas and the node, not as the path (Akshil, 2026-10-30):
   *  the card's read-only WHERE pill. Canvas pages only; elsewhere the card
   *  shows the locked path as the Tasks page's own does. */
  where?: { canvas: string; node: string | null };
}

/** How long the + waits on the schedule before opening the card without it
 *  (CodeRabbit on #1998): a read that hangs — not fails — would otherwise
 *  hold the button disabled until the browser gave up on the request. */
const SCHEDULE_WAIT_MS = 8000;

/** A seat's door: a draft's New task card (`draftHref`, as the List's row
 *  press), every other row's chat (`taskHref`, on the pulse's live status).
 *  Null while a pulse-only draft waits for the listing to name it. */
function seatHref({ pulse: t, row }: RailRow): string | null {
  const merged = row ? { ...row, status: t.status } : t;
  return (row && isDraftTask(row) ? draftHref(row) : null) ?? taskHref(merged);
}

/** The schedule's answer for the card, or empty if the read fails or outlasts
 *  `SCHEDULE_WAIT_MS` — either way the card opens (see `openNewTask`). */
function cardSeed(): Promise<{ modes: string[]; recents: string[] }> {
  const empty = { modes: [] as string[], recents: [] as string[] };
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => resolve(empty), SCHEDULE_WAIT_MS);
    getSchedule().then(
      (s) => {
        window.clearTimeout(timer);
        resolve({ modes: s.permission_modes, recents: s.entries.map((e) => e.target) });
      },
      () => {
        window.clearTimeout(timer);
        resolve(empty);
      },
    );
  });
}

export function ClaudeRail({
  place,
  newTask,
  git,
  activeSessionId,
  onOpen,
  onOpenChat,
  onSelectNode,
  onPullStart,
  onCardPinned,
}: {
  /** Where the rail stands (claude-rail-lib `RailPlace`): whose chats ride
   *  it. On a canvas the selected node's file narrows it (header). The
   *  caller memoises it: the seats derive from it. */
  place: RailPlace;
  /** What the + makes a task on (`NewTaskSeed`). */
  newTask: NewTaskSeed;
  /** THE GIT SECTION (owner, 2026-11-02: "we need to show git as well when
   *  collapsed … like the collapsed left sidebar where we have sections"):
   *  under the chats, past a separator, the way the left rail splits
   *  Projects / Canvases / Favourites — the branch, a Check-for-updates seat,
   *  and the last three commits that touched `path` (rail-git.ts). Drawn only
   *  where `path` sits in a repository; absent on a canvas (a clone is not
   *  one). `onOpenGit` is the branch's and the commits' door: the page's git
   *  panel (the explorer's git companion, the app page's git peek). */
  git?: { path: string; onOpenGit?: () => void };
  /** The session the page's URL names, drawn as the current row. */
  activeSessionId: string | null;
  /** Open the pane as it is. */
  onOpen: () => void;
  /** Open one of the canvas's chats: its Tasks-page door (`taskHref`). */
  onOpenChat: (href: string) => void;
  /** The card's node chip: select that node in the workbench (RailPeek);
   *  canvas pages only — without it the chip is a label and the foot's
   *  canvas seat is not drawn. Null — the rail's last seat while a node is
   *  selected (header) — asks
   *  it to deselect, back to the whole canvas. */
  onSelectNode?: (udfName: string | null) => void;
  /** A pointer down on the rail's inner edge: the start of a pull that opens
   *  the pane at the width the pointer drags it to (CanvasWorkspace
   *  `onRailPull`). */
  onPullStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
  /** A card opened by a press is up (`true`) or gone (`false`): the page
   *  holds its frame's focus-on-enter meanwhile (header). */
  onCardPinned?: (pinned: boolean) => void;
}) {
  const rows = useTasksPulseRows();
  const peekOn = useCanvasRailPeek();
  // THE GIT SECTION'S FACTS (header; rail-git.ts). Read for no path on a
  // rail with no git — the hook is inert then.
  const railGit = useRailGit(git?.path ?? null);
  // THE CANVAS PLACE'S OWN FACTS (header): the selected node, as the
  // workbench names it, and the canvas's name — what the foot's canvas seat
  // and the +'s words read. Null / the place's own name elsewhere.
  const node = useMemo(
    () => (place.kind === "canvas" && place.file ? { udfName: nodeName(place.file), file: place.file } : null),
    [place],
  );
  const name = place.kind === "canvas" ? place.name : placeName(place);
  // Drafts and scheduled rows ride the rail whatever the card flag says
  // (owner, 2026-10-30: "the flag is only for the preview thing; the fixes
  // we made — order etc. — should be there without the flag"): the rail is
  // the List's order in rings, drafts included (claude-rail-lib
  // `sessionless`) — every page's rail, so it is set here, not by the page.
  const seatPlace = useMemo((): RailPlace => ({ ...place, sessionless: true }), [place]);
  const scope = placeName(place);
  // THE CAPTION'S WORDS ARE THE LISTING'S, not the pulse's: the pulse carries
  // status and a title, but the list row's caption is built from the full row
  // (recap, last reply, last message, the draft) — and so is the List's ORDER
  // (sortForList reads the full row).
  //
  // FOLLOWED LIVE, not read once (owner, 2026-10-30: "if I make the draft in
  // the card it doesn't update instantly … the sidebar should get updates
  // instantly as well"): the rail subscribes to the listing feed the Tasks
  // page and the pane's own Recent list follow (`subscribeListing`, the
  // `/api/tasks/changes` long-poll, refcounted to one transport per
  // document), so a draft typed into the card's composer, a rename, a chip
  // — anything the listing carries — reaches the rings the moment the
  // server announces it. The read used to be a one-shot `getTasks` repeated
  // when a row's STATUS moved, which a draft never moves: the dot waited for
  // the next pane open. The pulse poll stands down while the feed is live,
  // as it does for every other follower. A failed read keeps the rows on
  // screen (the feed says `failed`; the last good rows are still the best
  // answer).
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(
    () =>
      subscribeListing((ev) => {
        if (!ev.failed) setListing(ev.rows);
      }),
    [],
  );
  // A DRAFT RECORD MOVED (the feed's draft channel: a composer's unsent words
  // saved or cleared, a New task card's form) — the rows' `draft` join is a
  // listing fact, so re-read it now rather than on the next row movement.
  // Collapsed to one read per tick by `refreshListing` itself.
  useEffect(() => onDraftChange(() => refreshListing()), []);
  const full = useMemo(() => new Map((listing ?? []).map((t) => [t.key, t])), [listing]);

  // THE RAIL'S SEATS, in the List's order (claude-rail-lib). `now` is read
  // once per derivation, as `sortForList` asks; the derivation re-runs when a
  // row moves (the pulse) or the listing re-reads, which is when a rank can
  // change.
  // THE FAVOURITES (header) draw in their own section, and ONLY there: the
  // list leaves a starred chat out (`listSeats`). Behind the favourites
  // switch (favourites-flag) AND the card's (the star lives on the card;
  // without it there is nothing to star with): off, no section, no
  // favourite's card, and a starred ring is a ring in the list like any
  // other.
  const favsOn = useTaskFavourites() && peekOn;
  const favsOnRef = useRef(favsOn);
  favsOnRef.current = favsOn;
  const favs = useMemo(() => (favsOn ? favSeats(rows, full) : []), [favsOn, rows, full]);
  const allSeats = useMemo(
    () => listSeats(railRows(rows, full, seatPlace), favsOn),
    [rows, full, seatPlace, favsOn],
  );
  // ONLY AS MANY RINGS AS FIT, from the top — no scroll (owner, 2026-11-02:
  // "if we have more chats let's not add scroll, just clip them; basically we
  // only show the top chats"). The list is its rings' height and the foot
  // stands right under the last ring (owner, 2026-11-03: "the back to canvas
  // button should be end of list"); the column's leftover is the ROOM, an
  // empty box under the foot (claude-rail.css `.claude-rail-room`). How many
  // whole seats the column holds is measured off the two together — the
  // list's height plus the room's, the list's row gap, one seat's height —
  // and re-measured when either resizes, so a taller window shows more and a
  // shorter one fewer, never half a ring. (The list alone would not do: once
  // cut to the rings that fit, it is their height, and a window growing
  // taller would leave it there.) `Infinity` until the first measure: the
  // list must exist to be measured, and a first paint with every ring is
  // clipped by the box the same frame. A favourite starred or dropped
  // (the foot, below) resizes the room, which the observer hears.
  const listRef = useRef<HTMLDivElement | null>(null);
  const roomRef = useRef<HTMLDivElement | null>(null);
  const [fit, setFit] = useState<number>(Infinity);
  const anyRing = allSeats.length > 0;
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const measure = () => {
      const cs = getComputedStyle(el);
      // Less the list's own padding (claude-rail.css: 2px of room for the
      // first and last ring's wash), which is no room for a ring.
      const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
      const h = el.clientHeight - pad + (roomRef.current?.clientHeight ?? 0);
      const gap = parseFloat(cs.rowGap) || 0;
      const seat = el.firstElementChild?.getBoundingClientRect().height || 28;
      setFit(Math.max(0, Math.floor((h + gap) / (seat + gap))));
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    if (roomRef.current) ro?.observe(roomRef.current);
    return () => ro?.disconnect();
  }, [anyRing]);
  const baseSeats = useMemo(() => (fit < allSeats.length ? allSeats.slice(0, fit) : allSeats), [allSeats, fit]);

  // THE DOTS A PRESS CLEARED (header; claude-rail-lib `ReadMark`): held
  // while the pulse still reports what the press saw, spent the moment it
  // says anything else. Pruned against the rows the rail draws from, so a
  // stale mark never outlives its row.
  const [marks, setMarks] = useState<ReadonlyMap<string, ReadMark>>(() => new Map());
  const marksRef = useRef(marks);
  marksRef.current = marks;
  useEffect(() => {
    setMarks((m) => keepMarks(m, rows));
  }, [rows]);
  const dropMark = useCallback((key: string, mark: ReadMark) => {
    setMarks((m) => {
      if (m.get(key) !== mark) return m;
      const next = new Map(m);
      next.delete(key);
      return next;
    });
  }, []);
  const clearUnread = useCallback(
    (t: TaskPulseTask) => {
      // Hollow already (a press while the pulse has yet to agree): nothing
      // to clear, and no second write.
      if (dotCleared(marksRef.current.get(t.key), t)) return;
      const mark = readMark(t);
      if (!mark) return;
      setMarks((m) => new Map(m).set(t.key, mark));
      // Not awaited: the press is opening the chat and must not wait on the
      // write. A refusal — or a write still out past READ_WAIT_MS — puts
      // the dot back; unless a later press replaced the mark, whose own
      // write is the one that counts. A confirmed write leaves the mark to
      // the rows (`keepMarks`): it ends on the row's own word (the zero),
      // never on a clock — the listing feed can run up to its 20s floor
      // behind the write, and a dot that came back meanwhile would be a
      // lie. A mark cannot outlive its truth: a message is unread only
      // once it has HAPPENED, so a real new dot always moves `happened_at`.
      const deadline = new Promise<never>((_, reject) => {
        window.setTimeout(() => reject(new Error("read timed out")), READ_WAIT_MS);
      });
      void Promise.race([markWholeTaskRead(t.key), deadline]).catch(() => dropMark(t.key, mark));
    },
    [dropMark],
  );

  // THE HOVER CARD (header; RailPeek.tsx). `peekKey` is what the card shows,
  // `kept` the chats that stay mounted behind it (newest first), both written
  // by the intent machine and nothing else. The ring elements are kept by key
  // for the card's placement and for handing focus back on Escape.
  const [peekKey, setPeekKey] = useState<string | null>(null);
  const refocusOn = useRef<string | null>(null);
  const [pinned, setPinned] = useState(false);
  // HELD (owner, 2026-11-02; every pin since 2026-11-03): a held card ignores
  // the outside press, the press into the workbench frame, the all-chats seat
  // and Escape — only its × or its Open door let go. Set wherever `pinned`
  // becomes true (`pin` below): a ring press, a press inside the card, the
  // keyboard's way in. Dropped with the card.
  const [held, setHeld] = useState(false);
  const heldRef = useRef(false);
  heldRef.current = held;
  const [kept, setKept] = useState<readonly string[]>([]);
  const intentRef = useRef<PeekIntent | null>(null);
  if (!intentRef.current) {
    intentRef.current = new PeekIntent({
      show: (key) => {
        // A swap while the reader is IN the card (typing in a pinned card's
        // composer, then pressing another ring): the
        // chat they were in is about to hide, focus and all — take the
        // keyboard to the new chat's composer (the effect below). Let go of
        // the old one NOW: a hidden chat is not inert, and keys typed before
        // the new composer takes over would land — Enter would send — in the
        // chat that just left view (Bugbot on #2040).
        const card = document.getElementById(PEEK_ID);
        const a = document.activeElement;
        refocusOn.current = key && a && card?.contains(a) ? key : null;
        if (refocusOn.current && a instanceof HTMLElement) a.blur();
        setPeekKey(key);
      },
      // The intent calls `pin(false)` only as the card goes (dismiss, prune);
      // `held` drops with it (the effect below).
      pin: (on) => {
        setPinned(on);
        // THE STICKY CLICK is the favourites switch's (header): off, a
        // clicked card is pinned, not held — an outside press closes it.
        if (on && favsOnRef.current) setHeld(true);
      },
      warm: (key) => setKept((s) => keepAlive(s, key)),
    });
  }
  const intent = intentRef.current;
  useEffect(() => () => intent.dispose(), [intent]);
  useEffect(() => {
    if (!peekKey) setHeld(false);
  }, [peekKey]);
  // A HELD CHAT KEEPS ITS RING — OUTSIDE THE LIST (owner, 2026-11-02: "don't
  // make it the top item; we have a separator and then a canvas button at
  // the bottom — put it above the canvas button, after the separator, so we
  // know it doesn't belong to the list"). The rail narrows to the node; the
  // held chat, if it is not the node's, draws in the foot under the
  // hairline, above the canvas glyph, so the card keeps its anchor and the
  // reader sees what they pinned without mistaking it for one of the node's
  // chats. Letting go (the ×, Open) drops it with the next derivation;
  // archiving lets go first (RailPeek `onFiled`).
  // …AND A HELD CHAT CLIPPED OUT of the list (above) draws in the foot the
  // same way: still anchored, still seen.
  //
  // BUT NEVER ACROSS PLACES: the explorer keeps this rail mounted from one
  // folder or file to the next, and a card pinned on the last place is not
  // this place's to hold — by key alone it would ride into the foot here
  // (Bugbot on #2069). A place change lets the card go. A NODE SELECTION IS
  // NOT A PLACE CHANGE (owner, 2026-11-03: the canvas chat pinned, a node
  // selected — "we showed it with a button above back to canvas button and
  // we didn't hide or close that chat"): the canvas `place` is remade with
  // the node's file, but it is the same canvas, so the held card stays and
  // its ring rides into the foot (`heldExtra`). Compared by `placeRoot`,
  // which leaves the node out.
  //
  // EXCEPT A FAVOURITE'S OPEN CARD (header): it was not opened on this place,
  // it is the reader's, and it travels (owner, 2026-11-03).
  const root = placeRoot(place);
  const rootRef = useRef(root);
  useEffect(() => {
    if (rootRef.current === root) return;
    rootRef.current = root;
    setHeld(false);
    intent.dismiss();
  }, [root, intent]);
  const heldKey = held ? peekKey : null;
  const heldExtra = useMemo((): RailRow | null => {
    // A favourite's held card (a hover on its ring, then a press inside) has
    // its ring in the favourites section already.
    if (!heldKey || baseSeats.some((s) => s.pulse.key === heldKey) || favs.some((s) => s.pulse.key === heldKey)) return null;
    const pulse = rows.find((t) => t.key === heldKey) ?? full.get(heldKey) ?? null;
    return pulse ? { pulse, row: full.get(heldKey) ?? null } : null;
  }, [baseSeats, heldKey, rows, full, favs]);
  // Every seat this rail's card may show — the list's, plus the held
  // outsider, plus the favourites (a hover on their rings, header) — for the
  // card's anchors, the kept mounts and the prune; the LIST itself is
  // `baseSeats`, the outsider draws in the foot and the favourites in their
  // own section (below). A favourite's OPEN card is the shell's
  // (FavouritePeek), never this one.
  const seats = useMemo(
    () => (heldExtra || favs.length > 0 ? [...baseSeats, ...(heldExtra ? [heldExtra] : []), ...favs] : baseSeats),
    [baseSeats, heldExtra, favs],
  );
  const favKeys = useMemo(() => new Set(favs.map((s) => s.pulse.key)), [favs]);
  const seatsRef = useRef(seats);
  seatsRef.current = seats;
  // THE LAST RING PRESSED (owner, 2026-11-02): "the active css to the last
  // selected task… keep by task id so it is easier to spot; it doesn't
  // change when we filter tasks based on the selected node, only when we
  // click a task item". A press on a ring, either door; nothing else moves
  // it, and it survives the rail narrowing to a node and widening again.
  // Shared with the pane's Recent chats list (platform/lib/last-chat), so
  // the mark holds across the pane opening and shutting. THE FAVOURITES'
  // SECTION KEEPS ITS OWN (header, `useLastFav`), shared the same way with
  // the pane's Favourites block.
  const lastPressed = useLastChat();
  const lastFavPressed = useLastFav();
  const railRef = useRef<HTMLElement | null>(null);
  const peekRef = useRef<RailPeekHandle | null>(null);
  const ringEls = useRef(new Map<string, HTMLElement>());
  const anchorOf = useCallback((key: string) => ringEls.current.get(key) ?? null, []);
  // The chat's chunk, ahead of the first hover (ChatMount `preloadChat`).
  const anySeat = seats.length > 0;
  useEffect(() => {
    if (peekOn && anySeat) preloadChat();
  }, [peekOn, anySeat]);
  // The flag turned off: nothing to show. The seats changing is the
  // intent's to hear (`prune`): a ring that unmounts under the pointer never
  // fires a leave, and a chat that left the rail (archived, the node changed)
  // takes its card with it; the kept set drops the same keys so a stale chat
  // never holds one of the three warm slots.
  const peekSeat = peekOn ? seats.find((s) => s.pulse.key === peekKey) ?? null : null;
  useEffect(() => {
    if (!peekOn && peekKey) intent.dismiss();
  }, [peekOn, peekKey, intent]);
  // THE OPEN FAVOURITE (header): this window's (platform/lib/last-chat
  // `useFavOpen`) — instant, never another window's. That card is the
  // shell's (FavouritePeek), not this rail's: here the key only decides which
  // ring answers as the open favourite's (`is-open`, the caption,
  // `aria-controls`, no hover card), and that this rail's own card lets go of
  // the chat the favourite's card is taking (`letGo`, below). A key in the
  // section counts — and, for FAV_SETTLE_MS after this window opened it, a
  // key whose ring the pulse has not moved out of the LIST yet (a star
  // pressed in this rail's card: rail-peek-lib `railOpenFav`), so that list
  // ring never opens this rail's card on the chat the favourite's card holds.
  // An unstarred or archived key has no ring (FavouritePeek clears it).
  const favOpenKey = useFavOpen();
  const favOpenAt = favsOn && favOpenKey && !favKeys.has(favOpenKey) ? favOpenedAtMs(favOpenKey) : null;
  // The settle running out re-asks (the ring goes back to an ordinary one).
  const [, setFavSettled] = useState(0);
  useEffect(() => {
    if (favOpenAt === null) return;
    const ms = favOpenAt + FAV_SETTLE_MS - Date.now();
    if (ms <= 0) return;
    const id = window.setTimeout(() => setFavSettled((n) => n + 1), ms + 20);
    return () => window.clearTimeout(id);
  }, [favOpenAt]);
  const openFavKey = favsOn ? railOpenFav(favOpenKey, favKeys, favOpenAt, Date.now()) : null;
  // THE FAVOURITE'S CARD IS THE SHELL'S (FavouritePeek, mounted once above
  // every page): this rail only registers itself (fav-peek-store) so that
  // card can stand at its foot, and keeps its own card clear of that one
  // (`avoid`). A press on a favourite —
  // its ring in the favourites section, its only one — OPENS THAT CARD (this
  // window's open favourite, `useFavOpen`), never this rail's: "two different
  // previews".
  const { favBox } = useFavPeekState();
  // THE PAGE'S CANVAS, for both cards' where-chips (claude-rail-lib
  // `chipDoor`): on the canvas page a chip naming this canvas selects in
  // place. A getter over a ref: the favourite's card reads it at its press
  // (fav-peek-store `railHere`), and the selected node moves often.
  const hereRef = useRef<RailHere | null>(null);
  hereRef.current =
    place.kind === "canvas" && onSelectNode
      ? { env: place.env, name: place.name, file: place.file ?? null, select: onSelectNode }
      : null;
  const hereOf = useCallback(() => hereRef.current, []);
  useEffect(() => {
    const el = railRef.current;
    return el ? registerRail(el, hereOf) : undefined;
  }, [hereOf]);
  // ONE CHAT, ONE MOUNT: this rail's card lets go of the chat the
  // favourite's card is taking — on a favourite ring's press
  // (`openFavourite`), when this card's own star is pressed on (`onStarred`,
  // at once), and again when this window's open key moves (the node chat's
  // frame, say): two live mounts of one session would be two composers on
  // one draft. A card that held the keyboard hands it to
  // the chat's ring, never to the body.
  const letGo = useCallback(
    (key: string) => {
      // The unsent words first: the favourite's card mounts its own composer
      // on the pulse, and the rail's mount flushes on unmount — a commit
      // later, over what the new composer already read (RailPeek's own
      // leave-flush, for this hand-over).
      const seat = seatsRef.current.find((s) => s.pulse.key === key);
      const sid = seat?.pulse.session_id;
      if (sid) peekDraftSyncer(chatDraftKey(sid, seat.row?.target || seat.pulse.target || seat.pulse.project))?.flushNow();
      // A hover (or the press's own focus) still waiting to open this card
      // on the chat: dropped, or it opens a beat after the favourite's.
      intent.forget(key);
      if (intent.current === key) {
        const had = !!document.getElementById(PEEK_ID)?.contains(document.activeElement);
        setHeld(false);
        // Muted for the ring's focus when the keyboard goes back to it.
        intent.dismiss(had);
        // The chat is a favourite now (starred, or its card opened): its
        // ring is in the favourites section, not the list (Bugbot on #2100).
        if (had) focusRail(railRef.current, key, "foot");
      }
      setKept((k) => (k.includes(key) ? k.filter((x) => x !== key) : k));
    },
    [intent],
  );
  // The open key moving: in the layout phase, so the draft flush (`letGo`)
  // lands before the favourite's composer paints.
  useLayoutEffect(() => {
    if (openFavKey) letGo(openFavKey);
  }, [openFavKey, letGo]);
  // …AND A CHAT STARRED ANYWHERE while this card shows it (another window's
  // star): the list no longer holds it (header, `listSeats`), so this card
  // lets go and the ring is the favourites section's. Only on the TURN to
  // starred while shown: a favourite's ring now opens this card on a hover
  // (header), and that card must stay. In the layout phase, so the held
  // outsider's ring (`heldExtra`) never paints beside the section's — and
  // while it is still a seat, so `letGo` finds its draft to flush.
  const peekStarred = favsOn && !!peekKey && rows.some((t) => t.key === peekKey && t.starred);
  const peekWas = useRef<{ key: string | null; starred: boolean }>({ key: null, starred: false });
  useLayoutEffect(() => {
    const was = peekWas.current;
    peekWas.current = { key: peekKey, starred: peekStarred };
    if (peekKey && was.key === peekKey && !was.starred && peekStarred) letGo(peekKey);
  }, [peekKey, peekStarred, letGo]);
  // A star pressed on in this rail's card: the ring moved to the favourites
  // section, so this card lets go now, and the favourite's card opens in
  // this window — a star you just pressed is a card you want to see
  // (rail-peek-lib `railOnStarred`).
  const onStarred = useMemo(() => railOnStarred(() => favsOnRef.current, letGo, setFavOpen), [letGo]);
  // A FAVOURITE'S RING PRESSED (header): this window's favourite card opens
  // on it at once — a local write, nothing to wait for — and this rail's card
  // lets go of the chat first. A key that is not a live favourite (unstarred
  // a moment ago elsewhere, archived) opens no card: the ring's door instead.
  // A keyboard press asked for the favourite card's × (fav-peek-store
  // `pendingFocus`).
  const openFavourite = useCallback(
    (t: TaskPulseTask, href: string) => {
      if (!t.starred || t.status === "archived" || !t.session_id) {
        requestFavFocus(null);
        openChatRef.current(href);
        return;
      }
      letGo(t.key);
      setFavOpen(t.key);
    },
    [letGo],
  );
  // JSON, not a joined string: a key is free to contain any delimiter.
  const seatKeys = JSON.stringify(seats.map((s) => s.pulse.key));
  useEffect(() => {
    const live = new Set(JSON.parse(seatKeys) as string[]);
    intent.prune(live);
    setKept((k) => (k.every((key) => live.has(key)) ? k : k.filter((key) => live.has(key))));
  }, [seatKeys, intent]);
  const keptSeats = useMemo(
    () =>
      peekOn
        ? kept.map((k) => seats.find((s) => s.pulse.key === k)).filter((s): s is NonNullable<typeof s> => !!s)
        : [],
    [peekOn, kept, seats],
  );
  // While the card is up: a press anywhere but the rail and the card closes
  // it. A click INTO the workbench frame is not a press this document sees,
  // and it needs no rule of its own: the pointer is on the canvas, off the
  // ring and the card, so the grace close runs — and a card that was holding
  // the keyboard loses it to the frame (`onBlurCapture` → hold(false)), which
  // starts the same grace. There used to be a window-`blur` dismiss here; the
  // canvas wrapper focuses its frame on pointer ENTER (`focusCanvasOnEnter`),
  // and the 8px gap between rail and card is over that wrapper, so crossing
  // to the card blurred the window and killed the card mid-crossing (Bugbot).
  // A pinned card is the one exception (header): it holds the frame's
  // focus-on-enter off, so a blur onto the frame IS a press on the canvas,
  // and closes it (the pinned-card effect below).
  //
  // "INSIDE THE CARD" IS REACT'S ANSWER, NOT THE DOM'S: the chat's menus and
  // pickers are portals — React descendants of the card, DOM children of
  // body — so `contains` would call a press on a menu item an outside press
  // and close the card under it. The card's own capture handler
  // (RailPeek `onPressInside`) runs for those too; this native listener runs
  // first, so it defers its ruling by a tick and reads the flag then.
  const insidePress = useRef(false);
  const onPressInside = useCallback(() => {
    insidePress.current = true;
    intent.pressStart();
  }, [intent]);
  //
  // A PINNED card exempts only the rings' list — a ring's own press pins or
  // swaps the card (`onClick`); the gaps between rings and the list's
  // scrollbar are near misses — and the bridge over the gap to the card
  // (RailPeek), which reads as the card's edge. The rail's other seats (the
  // +, the panel button, the seam) dismiss it like any outside press:
  // nothing else ever closes a pinned card. An unpinned (hover) card exempts
  // the whole rail; the pointer leaving the ring does the closing there.
  useEffect(() => {
    if (!peekKey) return;
    let tick = 0;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      const el = t instanceof Element ? t : null;
      if (el?.closest(".claude-rail-peek-bridge")) return;
      // A held card is not closed by any press outside it (header).
      if (heldRef.current) return;
      if (pinned) {
        if (el && railRef.current?.querySelector(".claude-rail-chats")?.contains(el)) return;
      } else if (railRef.current?.contains(t)) return;
      insidePress.current = false;
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        if (!insidePress.current) intent.dismiss();
      }, 0);
    };
    const onUp = () => intent.pressEnd();
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
    return () => {
      window.clearTimeout(tick);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
    };
  }, [peekKey, intent, pinned]);
  // NO ROOM FOR A CARD (RailPeek `onNoRoom`: a window too short or narrow):
  // the rings keep their one-line caption until a resize gives the card room
  // again — a hover that shows nothing at all is the one outcome to avoid.
  // A PRESS that found no room is answered the way the
  // press answers with no room from then on: it opens the chat in the pane
  // (`pressedHref`, set on the press, read during the commit it causes —
  // RailPeek places in a layout effect — and dropped on the next frame, so
  // a later resize that squeezes the card out only closes it).
  const [noRoom, setNoRoom] = useState(false);
  const pressedHref = useRef<string | null>(null);
  const onNoRoom = useCallback(() => {
    intent.dismiss();
    setNoRoom(true);
    const href = pressedHref.current;
    pressedHref.current = null;
    if (href) openChatRef.current(href);
  }, [intent]);
  useEffect(() => {
    if (!noRoom) return;
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    return () => window.removeEventListener("resize", re);
  }, [noRoom]);
  // …and when the favourite's card moves or goes (its `avoid` box is part
  // of the room): a window that was short with it up is not without it.
  useEffect(() => {
    setNoRoom(false);
  }, [favBox]);
  const dismissToRing = useCallback(() => {
    const key = intent.current;
    intent.dismiss(true);
    if (key) ringEls.current.get(key)?.focus({ preventScroll: true });
  }, [intent]);
  // Focus into the card once it is drawn: the class lands a frame after the
  // show, and a focus on a `visibility: hidden` box does nothing, so try for
  // a few frames rather than guess the one.
  const focusCardSoon = useCallback(() => {
    let tries = 0;
    const attempt = () => {
      const card = document.getElementById(PEEK_ID);
      if (peekRef.current?.focusChat() && card?.contains(document.activeElement)) return;
      if (++tries < 12) requestAnimationFrame(attempt);
    };
    requestAnimationFrame(attempt);
  }, []);
  // A PRESS ON A RING PUTS THE KEYBOARD IN THE COMPOSER (owner, 2026-10-31:
  // "instead of opening the sidebar we focus the composer on click, for the
  // user ready to type"). The card is drawn a frame after the show and the
  // chat's chunk may still be loading, so retry until the composer exists —
  // never falling back to the card's first button (that is the ×) — and give
  // up after FOCUS_WAIT_MS. One wait at a time (a later press supersedes it);
  // it also ends the moment the card closes, or the reader does anything
  // themselves: ANY later press or key (capture, document-wide) is theirs —
  // the chat's ⋮ menu and pickers are portals, so "focus is inside the card"
  // cannot tell them apart from the wait's own focus (Bugbot on #2037).
  // Registered after the press that started the wait: that press, and the
  // Enter keydown that precedes an Enter-click, are already past.
  const composerWait = useRef<(() => void) | null>(null);
  const focusComposerSoon = useCallback(
    // `keys: false` — a swap carrying the keyboard over (below): the reader
    // may be mid-word, and their keys are the reason to finish the move.
    (key: string, keys = true) => {
      composerWait.current?.();
      let live = true;
      let raf = 0;
      const stop = () => {
        live = false;
        cancelAnimationFrame(raf);
        document.removeEventListener("pointerdown", stop, true);
        document.removeEventListener("keydown", stop, true);
        if (composerWait.current === stop) composerWait.current = null;
      };
      composerWait.current = stop;
      document.addEventListener("pointerdown", stop, true);
      if (keys) document.addEventListener("keydown", stop, true);
      const until = performance.now() + FOCUS_WAIT_MS;
      const attempt = () => {
        if (!live) return;
        if (intent.current !== key) return stop();
        const card = document.getElementById(PEEK_ID);
        // Focus went to a control outside the card (not the body, not the
        // pressed ring): the reader moved on, never pull it back
        // (CodeRabbit on #2037).
        const a = document.activeElement;
        if (a && a !== document.body && !card?.contains(a) && a !== ringEls.current.get(key)) return stop();
        if (peekRef.current?.focusComposer() && card?.contains(document.activeElement)) return stop();
        if (performance.now() < until) raf = requestAnimationFrame(attempt);
        else stop();
      };
      raf = requestAnimationFrame(attempt);
    },
    [intent],
  );
  useEffect(() => () => composerWait.current?.(), []);
  // …and after a swap that left the keyboard in the chat going out of view
  // (`refocusOn`, set in the intent's sink).
  useEffect(() => {
    const key = refocusOn.current;
    refocusOn.current = null;
    if (!key || key !== peekKey) return;
    if (seats.find((s) => s.pulse.key === key)?.pulse.session_id) focusComposerSoon(key, false);
    else focusCardSoon();
    // The ref is spent on the first run, so a later `seats` change is a no-op.
  }, [peekKey, seats, focusComposerSoon, focusCardSoon]);
  // ESCAPE WITH THE FOCUS NOWHERE closes a pinned card. The ring (below),
  // the card's head and its chat each take their own Escape; a press that
  // opened the card in an engine that does not focus a link on click (WebKit)
  // leaves the focus on the body, where none of those hears it — and no
  // pointer move will ever close a pinned card for them.
  useEffect(() => {
    if (!pinned || !peekKey) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target !== document.body && e.target !== document.documentElement) return;
      if (heldRef.current) return;
      e.preventDefault();
      dismissToRing();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pinned, peekKey, dismissToRing]);
  // A PRESS INTO THE WORKBENCH FRAME closes a pinned card (header): the page
  // hears it only as this window's blur with the frame left holding the
  // focus. A blur with the focus anywhere else — another app, another
  // window — keeps the card. Read a tick later: the engines disagree on
  // whether `activeElement` has moved yet when the blur fires.
  const pinnedUp = pinned && peekKey !== null;
  const pinnedRef = useRef(onCardPinned);
  pinnedRef.current = onCardPinned;
  useEffect(() => {
    if (!pinnedUp) return;
    pinnedRef.current?.(true);
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const a = document.activeElement;
        if (heldRef.current) return;
        if (a instanceof HTMLIFrameElement && !document.getElementById(PEEK_ID)?.contains(a)) intent.dismiss();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
      pinnedRef.current?.(false);
    };
  }, [pinnedUp, intent]);
  const onRingKey = (key: string) => (e: ReactKeyboardEvent<HTMLAnchorElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      // A deliberate way in, like a press: pinned.
      intent.showNow(key, true);
      focusCardSoon();
    } else if (e.key === "Escape" && intent.current && !heldRef.current) {
      // A held card ignores Escape here as everywhere (header).
      e.preventDefault();
      intent.dismiss();
    }
  };

  // THE NEW TASK CARD. Opened on the press, once the schedule has answered
  // what the card needs (`Card`, `cardSeed`); the press is inert while that
  // read is out, so a double-click cannot stack two cards. A schedule read
  // that fails or hangs still opens the card — the modal falls back to "auto" on an empty modes
  // list, and an empty recents list only means fewer suggestions — because a
  // + that does nothing is worse than a card with one fewer dropdown option.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [card, setCard] = useState<Card | null>(null);
  const [opening, setOpening] = useState(false);
  // The all-chats seat pressed by keyboard (below): when the node goes, the
  // keyboard goes to the +, the rail's first seat, rather than to the body.
  const allRef = useRef<HTMLButtonElement | null>(null);
  const plusRef = useRef<HTMLButtonElement | null>(null);
  const refocusPlus = useRef(false);
  // THE POINTER'S RESTING PLACE AT THE SEAT'S PRESS. The press reshapes the
  // rail (the seat goes, the list grows), and the engine then reports a
  // pointer ENTER on whichever ring slid under the unmoved pointer — which
  // would open that chat's hover card over a press that asked for no chat.
  // A ring entered at this very spot is ignored until the pointer has
  // moved; the first real move clears it (`enterRing` below).
  const stillAt = useRef<{ x: number; y: number } | null>(null);
  const lastPointer = useRef<{ x: number; y: number } | null>(null);
  // …AND ANY RESHAPE OF THE RINGS under a pointer resting on the rail: a
  // favourite unstarred (its foot ring goes, the one above slides down into
  // its place), a chat starred, a pulse's new row. The ring that slid under
  // the unmoved pointer is not a hover (owner, 2026-11-04: "when I
  // unfavourite, a random task from above opens"). In the layout phase: the
  // engine's enter comes after the paint.
  const ringOrder = JSON.stringify([seats.map((x) => x.pulse.key), favs.map((x) => x.pulse.key)]);
  const ringsWere = useRef(ringOrder);
  useLayoutEffect(() => {
    if (ringsWere.current === ringOrder) return;
    ringsWere.current = ringOrder;
    if (lastPointer.current) stillAt.current = lastPointer.current;
  }, [ringOrder]);
  const enterRing = (key: string, e: ReactPointerEvent<HTMLAnchorElement>) => {
    const at = stillAt.current;
    if (at && Math.abs(e.clientX - at.x) < 2 && Math.abs(e.clientY - at.y) < 2) return;
    stillAt.current = null;
    intent.enterRing(key, "pointer");
  };
  useEffect(() => {
    if (node || !refocusPlus.current) return;
    refocusPlus.current = false;
    plusRef.current?.focus({ preventScroll: true });
  }, [node]);
  const waiting = newTask.waiting;
  const openNewTask = () => {
    if (opening || card || waiting) return;
    setOpening(true);
    // As they were at the press (header of `Card`).
    const at = newTask.target;
    const where = newTask.where;
    void cardSeed().then((got) => {
      if (!alive.current) return;
      setOpening(false);
      setCard({ seq: Date.now(), target: at, where, ...got });
    });
  };
  // A DRAFT'S DOOR, IN PLACE (header of `Card.draft`): the same New task card
  // the + opens, on the draft's own record — what the Tasks page's row press
  // does (Scheduled `openDraft`), without leaving the canvas. Read FRESH, both
  // kinds: the listing row's `form` can trail an edit made a moment ago, and a
  // card seeded on it would autosave the older words back over the newer.
  // Nothing is minted, and the card's autosave goes on writing the same
  // record. Needs no clone: the row names its own target and node.
  //
  // THREE ANSWERS from the read (capped at SCHEDULE_WAIT_MS, the +'s cap, so
  // a hung read never holds the rail's doors shut):
  //   * the record — the card opens on it (a chat draft with no record yet
  //     opens on its key, as the Tasks page's hop does);
  //   * the record is GONE (a task draft deleted elsewhere, the row not yet
  //     re-read) — no card, which would mint it again; the listing is re-read
  //     so the ring goes;
  //   * NO ANSWER (failed or timed out) — no card on a guess, which could
  //     autosave a blank form over the stored one: the press takes the
  //     Tasks page's door instead (`href`), which reads the record itself.
  const openDraftCard = (row: Task, href: string) => {
    if (opening || card) return;
    const chat = isChatDraftTask(row);
    if (!chat && !row.draft_id) return;
    setOpening(true);
    // Where the row lives (claude-rail-lib `railScope`): on a canvas the
    // card's WHERE pill says the canvas and the node; elsewhere the locked
    // path says it (`NewTaskSeed.where`).
    const scopeOf = railScope(row, place);
    const base = {
      target: row.target || newTask.target,
      where:
        place.kind === "canvas"
          ? { canvas: name, node: scopeOf.kind === "node" ? scopeOf.name : null }
          : undefined,
    };
    type Read = { kind: "seed"; seed: DraftSeed | null } | { kind: "gone" } | { kind: "unknown" };
    const stored = new Promise<Read>((resolve) => {
      const timer = window.setTimeout(() => resolve({ kind: "unknown" }), SCHEDULE_WAIT_MS);
      fetchDrafts().then(
        (all) => {
          window.clearTimeout(timer);
          if (!all) return resolve({ kind: "unknown" });
          if (chat) {
            const seed = chatHopSeed(row.key, all.chat[row.key] ?? null, row.project || row.file || "");
            return resolve({ kind: "seed", seed });
          }
          const form = all.task[row.draft_id!];
          resolve(form ? { kind: "seed", seed: { id: row.draft_id!, form: { ...form } } } : { kind: "gone" });
        },
        () => {
          window.clearTimeout(timer);
          resolve({ kind: "unknown" });
        },
      );
    });
    void Promise.all([cardSeed(), stored]).then(([got, read]) => {
      if (!alive.current) return;
      setOpening(false);
      if (read.kind === "gone") {
        refreshListing();
        return;
      }
      if (read.kind === "unknown") {
        onOpenChat(href);
        return;
      }
      setCard({
        seq: Date.now(),
        ...base,
        ...got,
        draft: read.seed,
        chatKey: chat ? row.key : "",
        time: reopenTime(read.seed),
      });
    });
  };
  // THE RAIL'S ONE DOOR: a draft's href opens its card here; every other
  // href is the page's (`onOpenChat`). By href, because that is what the
  // ring, the hover card's Open and the no-room press all hand over — and
  // every one of them moves the last-picked mark to that chat (header).
  const seatByHref = useMemo(() => {
    const map = new Map<string, RailRow>();
    for (const seat of seats) {
      const href = seatHref(seat);
      if (href) map.set(href, seat);
    }
    return map;
  }, [seats]);
  const openChat = (href: string) => {
    const seat = seatByHref.get(href);
    // A favourite's door (its hover card's Open) moves the favourites' mark.
    if (seat) (favKeys.has(seat.pulse.key) ? setLastFav : setLastChat)(lastChatId(seat.pulse));
    if (seat?.row && isDraftTask(seat.row)) openDraftCard(seat.row, href);
    else onOpenChat(href);
  };
  const openChatRef = useRef(openChat);
  openChatRef.current = openChat;
  // A RING'S ORDINARY PRESS (header): the card, shown now and pinned, with
  // the keyboard on its way to the composer — or, with no room for a card,
  // the chat in the pane. Also a favourite's press that the server refused
  // (`openFavourite`), which lands a round trip later: hence the ref.
  const pressRing = (t: TaskPulseTask, href: string, keyboard: boolean) => {
    if (peekOn && !noRoom) {
      pressedHref.current = href;
      requestAnimationFrame(() => {
        pressedHref.current = null;
      });
      // Shown now and pinned (header) — over a hover card of this ring it
      // only pins; over another's, it swaps.
      intent.press(t.key);
      hideHint();
      // Ready to type, whether it was a click or an Enter on a focused ring.
      if (t.session_id) focusComposerSoon(t.key);
      // A draft or scheduled row has no composer: an Enter still takes the
      // keyboard into the card.
      else if (keyboard) focusCardSoon();
      return;
    }
    openChat(href);
  };
  const pressRingRef = useRef(pressRing);
  pressRingRef.current = pressRing;
  const plusLabel = waiting ?? (newTask.on ? `New task on ${newTask.on}` : "New task");

  // ONE ACTIVE RING PER SECTION (header; rail-peek-lib `activeRing`). The
  // list's section is its rings plus the held outsider in the foot. With the
  // favourites off the list keeps every mark it had (flag-off parity).
  const listSection = heldExtra ? [...baseSeats, heldExtra] : baseSeats;
  const activeList = activeRing(
    listSection.map((s) => s.pulse.key),
    [
      pinned ? peekKey : null,
      lastPressed !== null ? listSection.find((s) => lastChatId(s.pulse) === lastPressed)?.pulse.key : null,
      activeSessionId ? listSection.find((s) => s.pulse.session_id === activeSessionId)?.pulse.key : null,
    ],
  );
  const activeFav = activeRing(
    favs.map((s) => s.pulse.key),
    [openFavKey, lastFavPressed !== null ? favs.find((s) => lastChatId(s.pulse) === lastFavPressed)?.pulse.key : null],
  );

  // ONE RING, as the list draws it — and as the foot draws the held outsider.
  const ringOf = ({ pulse: t, row }: RailRow, foot = false) => {
              // The live status is the pulse's (fresher than the listing).
              const merged = row ? { ...row, status: t.status } : t;
              // Hollow since the press, until the pulse agrees (header).
              const unread = t.unread > 0 && !dotCleared(marks.get(t.key), t);
              const line = cardTitleLine(merged);
              const reply = row && !recapTitlesOn(row) ? row.last_reply || "" : "";
              const label = line.text || t.title || "Chat";
              // A DRAFT'S DOOR is the New task card (`draftHref`, as the List's
              // row press); every other row's is the chat's (`taskHref`). A
              // pulse-only draft (the listing not held yet) has neither for a
              // moment: it still gets its ring, the press waits for the
              // listing.
              const draft = !!row && isDraftTask(row);
              const href = seatHref({ pulse: t, row });
              // No door, no ring (as before): a form-scheduled row with no
              // session, or a pulse-only draft the listing has not named yet.
              if (!href) return null;
              const words = draft ? "draft" : statusWords(unread ? t : { ...t, unread: 0 }, !!row?.failed);
              const peeked = peekOn && peekKey === t.key;
              // Each section's own last press (header): the favourites' mark
              // in their section, the list's everywhere else.
              const lastMark = foot ? lastFavPressed : lastPressed;
              const isLast = lastMark !== null && lastChatId(t) === lastMark;
              const sessionHere = !!activeSessionId && t.session_id === activeSessionId;
              // ONE ACTIVE RING PER SECTION, no persistent wash on a
              // favourite, no hover card on the open favourite's ring in
              // either section (claude-rail-lib `ringMarks`).
              const wears = ringMarks({
                key: t.key,
                foot,
                favsOn,
                peekOn,
                peekKey,
                pinned,
                held,
                openFavKey,
                sessionHere,
                isLast,
                activeList,
                activeFav,
              });
              // The rings' own captions step aside for the card: a hover
              // shows the card, and the card IS the caption. The exception
              // is a hover that shows nothing — another ring while a card is
              // pinned (owner, 2026-10-31: "show me the tooltip title so I
              // know what other chat it is at least"): that ring gets its
              // title back, the title alone.
              // …and the ring of the FAVOURITE whose own card is open in this
              // window (header) — in the foot, or still in the list a moment
              // after its star was pressed (`railOpenFav`): the hover is its
              // caption. Any other favourite's ring hovers like a list ring.
              const favRing = wears.favRing;
              const titleOnly = (peekOn && pinned && peekKey !== null && peekKey !== t.key) || favRing;
              const captionless = peekOn && !titleOnly;
              // The list's item is a wrapper, the link is a link: `aria-haspopup`
              // and `aria-expanded` are not allowed on `listitem`, and a link
              // that opens a dialog is the shape they describe.
              return (
                <div key={t.key} role="listitem" className="claude-rail-seat">
                <a
                  ref={(el) => {
                    // A favourite's ring anchors this rail's hover card
                    // (header); the favourite's own card stands on the
                    // rail's bottom edge (FavouritePeek).
                    if (el) ringEls.current.set(t.key, el);
                    else ringEls.current.delete(t.key);
                  }}
                  href={href}
                  // Found again by key when a card shuts under the keyboard
                  // (fav-peek-store `focusRail`).
                  data-task-key={t.key}
                  className={["sidebar-rail-btn", "claude-rail-chat", ...wears.classes].join(" ")}
                  aria-label={`${label} — ${words}`}
                  // The list row's own caption (platform/lib/hints): the
                  // title's line, then the reply's — what the row clamps.
                  // While the card stands in for it, an EMPTY hint is the
                  // opt-out (hints.ts `hintOf`): the card is the caption.
                  data-hint={captionless && !noRoom ? "" : line.hint || label}
                  data-hint-title={line.hint || label}
                  data-hint-reply={titleOnly ? "" : reply}
                  // Beside the ring, not at the pointer (platform/lib/hints
                  // `placeBeside`): a 48px column at the window's edge.
                  data-hint-side="left"
                  {...(peekOn && !favRing
                    ? {
                        "aria-haspopup": "dialog" as const,
                        "aria-expanded": peeked,
                        "aria-keyshortcuts": "ArrowLeft",
                        ...(peeked ? { "aria-controls": PEEK_ID } : {}),
                        onKeyDown: onRingKey(t.key),
                      }
                    : peekOn && favRing
                      ? {
                          // The open favourite's ring: its card is the
                          // shell's (FavouritePeek), a dialog of its own.
                          "aria-haspopup": "dialog" as const,
                          "aria-expanded": true,
                          "aria-controls": FAV_PEEK_ID,
                        }
                      : {})}
                  {...(peekOn
                    ? {
                        // The open favourite's ring opens no hover card, but
                        // a leave is always heard: the intent must not think
                        // the pointer still rests on a ring it left.
                        ...(!wears.hovers
                          ? {}
                          : {
                              onPointerEnter: (e: ReactPointerEvent<HTMLAnchorElement>) => enterRing(t.key, e),
                              // A focus HANDED here (a card shutting
                              // under the keyboard, fav-peek-store
                              // `focusRail`) opens no card.
                              onFocus: (e: ReactFocusEvent<HTMLAnchorElement>) =>
                                takeQuietFocus(e.currentTarget)
                                  ? intent.quietFocus(t.key)
                                  : intent.enterRing(t.key, "focus"),
                            }),
                        onPointerLeave: () => intent.leaveRing("pointer"),
                        onBlur: () => intent.leaveRing("focus"),
                      }
                    : {})}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    // Opened, by either door below: read (header), and the
                    // last ring pressed (header) — the favourites' own mark
                    // for a ring in their section, the list's otherwise.
                    clearUnread(t);
                    if (foot || favRing) setLastFav(lastChatId(t));
                    else setLastChat(lastChatId(t));
                    // A FAVOURITE'S PRESS — its ring in the section, the
                    // only ring it has — opens the shell's favourite card in
                    // this window (header), not this rail's, and lets this
                    // rail's hover card go. NEVER `pressRing` for it: a
                    // favourite's ring never pins this rail's card (claude-
                    // rail-lib `ringMarks`). The open favourite's ring — the
                    // foot's, or the list's a moment after its star — has its
                    // card up already: nothing more to open.
                    if ((foot || favRing) && peekOn) {
                      hideHint();
                      // A keyboard press: the favourite's card takes the
                      // keyboard once it stands (fav-peek-store); a pointer
                      // press takes nothing.
                      if (e.detail === 0) requestFavFocus(t.key);
                      if (!favRing) openFavourite(t, href);
                      return;
                    }
                    pressRing(t, href, e.detail === 0);
                  }}
                >
                  <StatusIcon
                    // The full row's column where it is held: a draft row is
                    // `draft` only once `kind` is known (the pulse has no kind).
                    status={taskColumn(merged)}
                    // With the card on, the ring's own native `title` tooltip
                    // ("Done", "Draft") would float over the card a second
                    // later; "" is no tooltip. The link's aria-label still
                    // names the chat and its state.
                    {...(captionless ? { label: "" } : {})}
                    unread={unread}
                    background={liveJobRow(t)}
                    // The red centre dot the List gives a draft row and a
                    // settled row holding unsent words (tasks-lib.draftRing).
                    draftHeld={!!row && draftRing(merged as Task)}
                    {...(row?.failed ? { failed: true } : {})}
                  />
                </a>
                </div>
              );
  };

  return (
    <aside
      ref={railRef}
      className={"claude-rail" + (favs.length > 0 ? " has-favs" : "")}
      aria-label="Claude"
      onPointerMove={(e) => {
        lastPointer.current = { x: e.clientX, y: e.clientY };
      }}
      // Off the rail, no resting place on it (`ringsMoved`).
      onPointerLeave={() => {
        lastPointer.current = null;
      }}
    >
      {/* THE RAIL'S INNER EDGE IS A SEAM: pulled left it opens the pane at the
          width the pointer takes it to — the explorer sidebar's reopen edge
          (SideReopenEdge) and the global sidebar's rail, the same gesture on
          the same kind of edge. The Show button stays the click. */}
      <div
        className="claude-rail-seam"
        role="separator"
        aria-orientation="vertical"
        aria-label="Drag to show the Claude panel"
        onPointerDown={onPullStart}
      />
      <button
        type="button"
        className="sidebar-rail-btn"
        title="Show the Claude panel"
        aria-label="Show the Claude panel"
        aria-expanded={false}
        onClick={onOpen}
      >
        <PanelIcon side="right" />
      </button>
      <div className="sidebar-rail-items">
        <button
          ref={plusRef}
          type="button"
          className="sidebar-rail-btn claude-rail-item"
          title={plusLabel}
          aria-label={plusLabel}
          aria-haspopup="dialog"
          aria-busy={opening || waiting !== null || undefined}
          disabled={opening || waiting !== null}
          onClick={openNewTask}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        {anyRing && (
          <div
            ref={listRef}
            className="claude-rail-chats"
            role="list"
            aria-label={`Chats on ${scope}${allSeats.length > baseSeats.length ? ` (${baseSeats.length} of ${allSeats.length} shown)` : ""}`}
          >
            {baseSeats.map((seat) => ringOf(seat))}
          </div>
        )}
        {(node || heldExtra) && (
          // THE FOOT, under the list's hairline: the held outsider's ring
          // (header), then the way back to the whole canvas — the canvas
          // glyph, which deselects the node in the workbench. BELOW the list,
          // not its last row: the list clips to the rings that fit, and a
          // way out that was clipped off is no way out.
          <div className="claude-rail-all-seat">
            {heldExtra && (
              <div className="claude-rail-held" role="list" aria-label={node ? "Pinned chat, not on this node" : "Pinned chat, past the list's room"}>
                {ringOf(heldExtra)}
              </div>
            )}
            {node && onSelectNode && (
            <button
              ref={allRef}
              type="button"
              className="sidebar-rail-btn claude-rail-item claude-rail-all"
              // No native `title` beside the app's hint (owner, 2026-11-02:
              // "there are two tooltips here — only have our one").
              aria-label={`All chats on ${name} (deselect ${node.udfName})`}
              data-hint={`All chats on ${name}`}
              data-hint-side="left"
              onClick={(e) => {
                hideHint();
                // A keyboard press: this button is about to unmount under
                // the focus, which would fall to the body — hand it to the
                // + once the node is gone (the effect above).
                refocusPlus.current = e.detail === 0;
                // A pinned card closes on this press like any other outside
                // press — from the keyboard too, which the document's
                // pointerdown rule never hears (Bugbot on #2049).
                if (intent.current && !heldRef.current) intent.dismiss();
                // Where the pointer rests now (the press's own spot, or the
                // last move for a keyboard press): a ring sliding under it
                // is not a hover (`stillAt`).
                stillAt.current = e.detail === 0 ? lastPointer.current : { x: e.clientX, y: e.clientY };
                onSelectNode(null);
              }}
            >
              <CanvasGlyph size={16} />
            </button>
            )}
          </div>
        )}
        {/* THE ROOM (header, `fit`): the column's leftover, measured, never
            drawn in — so the foot keeps to the list's end and the Git
            section to the column's. */}
        <div ref={roomRef} className="claude-rail-room" aria-hidden="true" />
        {git && railGit.repo && (
          // THE GIT SECTION (header): past the left rail's own separator
          // (sidebar.css `.sidebar-rail-sep`), the repository the page sits
          // in. Each seat wears the app's hint (platform/lib/hints), beside
          // the rail like the rings' — a 48px column has no room for words.
          <>
            <span className="sidebar-rail-sep" aria-hidden="true" />
            <div className="claude-rail-git" role="group" aria-label="Git">
              {(() => {
                const r = railGit.repo;
                const behind = r.behind ?? 0;
                const ahead = r.ahead ?? 0;
                const counts = !r.has_remote
                  ? "No remote"
                  : r.default_branch === null
                    ? "Default branch not known yet — check to find out"
                    : `${ahead} ahead · ${behind} behind ${r.default_branch}`;
                const checkWords = railGit.checking
                  ? "Checking for updates…"
                  : railGit.checked === false
                    ? "Check for updates — no fresh answer from the remote yet"
                    : "Check for updates";
                const branch = r.branch ?? "detached HEAD";
                // A seat with no door (no git panel on this page, no remote
                // to check) is a LABEL, not a disabled button: `disabled`
                // would swallow the pointer, and the hint is the only place
                // its words are. `aria-disabled` says so; the press does
                // nothing.
                const label = (on: boolean) => (on ? {} : { "aria-disabled": true as const, className: " is-label" });
                const door = label(!!git.onOpenGit);
                return (
                  <>
                    <button
                      type="button"
                      className={"sidebar-rail-btn claude-rail-item claude-rail-git-branch" + (door.className ?? "")}
                      aria-label={`On ${branch} · ${counts}${git.onOpenGit ? " — open the Git panel" : ""}`}
                      {...(door["aria-disabled"] ? { "aria-disabled": true } : {})}
                      data-hint={`On ${branch}`}
                      data-hint-title={`On ${branch}`}
                      data-hint-reply={counts}
                      data-hint-side="left"
                      onClick={() => {
                        hideHint();
                        git.onOpenGit?.();
                      }}
                    >
                      <GitBranch size={16} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className={
                        "sidebar-rail-btn claude-rail-item claude-rail-git-check" +
                        (railGit.checking ? " is-checking" : "") +
                        (label(r.has_remote).className ?? "")
                      }
                      aria-label={`${checkWords} · ${counts}`}
                      aria-busy={railGit.checking || undefined}
                      {...(r.has_remote ? {} : { "aria-disabled": true })}
                      data-hint={checkWords}
                      data-hint-title={checkWords}
                      data-hint-reply={counts}
                      data-hint-side="left"
                      onClick={() => {
                        hideHint();
                        if (r.has_remote && !railGit.checking) railGit.check();
                      }}
                    >
                      <RefreshCw size={16} aria-hidden="true" />
                      {behind > 0 && <span className="sidebar-rail-dot is-unread" aria-hidden="true" />}
                    </button>
                    {r.commits.map((c) => {
                      const when = timeAgo(c.when) ?? "";
                      const who = [c.author, when].filter(Boolean).join(" · ");
                      return (
                        <button
                          key={c.sha}
                          type="button"
                          className={"sidebar-rail-btn claude-rail-item claude-rail-git-commit" + (door.className ?? "")}
                          aria-label={`${c.subject} — ${who} (${c.short})`}
                          {...(door["aria-disabled"] ? { "aria-disabled": true } : {})}
                          data-hint={c.subject}
                          data-hint-title={c.subject}
                          data-hint-reply={`${who} · ${c.short}`}
                          data-hint-side="left"
                          onClick={() => {
                            hideHint();
                            git.onOpenGit?.();
                          }}
                        >
                          <GitCommitHorizontal size={16} aria-hidden="true" />
                        </button>
                      );
                    })}
                  </>
                );
              })()}
            </div>
          </>
        )}
        {favs.length > 0 && (
          // THE FAVOURITES, LAST (owner, 2026-11-03: "favourite should be
          // below git"): past their own hairline, one ring per starred chat
          // from anywhere — the chat the shell's favourite card stands
          // beside (`.is-open`). A ring here hovers like a list ring, bar the
          // open one (its caption stays); its press opens the favourite's
          // card.
          <>
            <span className="sidebar-rail-sep is-tight" aria-hidden="true" />
            <div
              className="claude-rail-favs"
              role="list"
              aria-label="Favourite chats"
              // Its floor (claude-rail.css): three rings, or every star when fewer.
              style={{ "--fav-floor": Math.min(favs.length, 3) } as CSSProperties}
            >
              {favs.map((seat) => ringOf(seat, true))}
            </div>
          </>
        )}
      </div>
      {peekOn && keptSeats.length > 0 && (
        <RailPeek
          ref={peekRef}
          id={PEEK_ID}
          shown={peekSeat}
          kept={keptSeats}
          anchorOf={anchorOf}
          railEl={railRef.current}
          place={place}
          onReload={refreshListing}
          onPointerEnter={() => intent.enterCard()}
          onPointerLeave={() => intent.leaveCard()}
          onCone={(inside) => (inside ? intent.enterCone() : intent.leaveCone())}
          onPressInside={onPressInside}
          onHold={(on) => intent.hold(on)}
          onDismiss={dismissToRing}
          onNoRoom={onNoRoom}
          onOpenChat={openChat}
          here={hereOf}
          held={held}
          onHeld={setHeld}
          onStarred={onStarred}
          avoid={favBox}
          pinned={pinned}
          onPin={() => {
            const key = intent.current;
            if (!key) return;
            intent.press(key);
            if (seats.find((s) => s.pulse.key === key)?.pulse.session_id) focusComposerSoon(key);
          }}
        />
      )}
      {card && (
        <NewJobModal
          // Keyed on the press: the card reads its props in `useState`
          // initialisers, so every opening must be a fresh mount
          // (Scheduled.tsx `openSeq`).
          key={card.seq}
          initialTime={card.time ?? null}
          // The draft's own record, when a draft row opened it (`Card.draft`).
          initialDraft={card.draft ?? null}
          chatKey={card.chatKey ?? ""}
          // SCOPED TO THE PLACE — the clone or the selected node's file in
          // it, the app folder, the explorer's folder or file — the project
          // page's Tasks tab's way (CanvasPage.tsx `scope.project`,
          // Scheduled.tsx `lockTarget`): a task made from a page is a task
          // about that page, so the path is stated, not asked (the + is held
          // until the page can say it).
          initialTarget={card.target}
          lockTarget={card.target !== null}
          // …and on a canvas SAID as the canvas and the node, not as that
          // path (Akshil, 2026-10-30): the card's read-only WHERE pill. The
          // canvas and node as they were at the press, the same snapshot as
          // the target.
          {...(card.where ? { lockedWhere: card.where } : {})}
          // From the List, not the calendar: a task typed here is one to run
          // now, and the when-row folds into More options.
          planning={!!card.time}
          permissionModes={card.modes}
          recentTargets={card.recents}
          // Closed without creating: the card may have left a DRAFT behind,
          // which the pulse never carries — re-read so its ring shows now.
          onClose={() => {
            setCard(null);
            refreshListing();
          }}
          // "Something changed, re-read": the pulse will move the new row
          // onto the rail; the listing is re-read now so its caption and
          // rank are not 600ms behind its ring.
          onCreated={() => refreshListing()}
        />
      )}
    </aside>
  );
}
