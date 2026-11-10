// THE RAIL (the owner's name for it, 2026-11-05): what stands at a page's
// right edge while its Claude pane is shut (owner, 2026-10-26: "a very min
// width column, like we have for the fused render left sidebar, just a single
// width column with the button") — and, with favourites on, BESIDE THE OPEN
// PANE too (below). Born on the canvas
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
//     screen at any moment, the explorer sidebar's own rule (SideChrome.tsx)
//     — so beside the open pane the rail draws no glyph (and no pull seam:
//     the pane's own seam resizes it).
//   - NEW TASK, at the head of the list (owner, 2026-10-28: "the + icon
//     should be at the top of the list and it should show a new task modal
//     when we click it, instead of opening the sidebar"). It opens the Tasks
//     page's own card (shell/NewJobModal) over the canvas, prefilled and
//     locked on the clone —
//     the pane stays shut, and the task lands on the rail through the pulse
//     like any other. (Beside the OPEN pane it is the pane's new chat
//     instead — below.)
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
//     Scrolls on a short window (favourites on; off, it clips to the rings
//     that fit).
//
// BESIDE THE OPEN PANE (owner, 2026-11-05: "we show the rail no matter what
// now … on every page … even if the sidebar is expanded or not. The only
// reason we don't show the rail is if there are no tasks in favourites AND no
// items to be shown in the rail"): with favourites on (favourites-flag), every
// page that draws this rail draws it whether its pane is shut or open
// (`paneOpen`), at the window's right edge past the pane. Open, the rail is
// the + and the list's rings on top, the favourites at its foot — and it is
// HIDDEN while it has neither a chat in its list nor a favourite (claude-rail-
// lib `railShown`; shut, it always stands: it is the way back in). A hidden
// rail registers nothing, so the favourite's card stands only beside a rail
// on screen. Beside the open pane, this rail's card mounts no chat the pane
// already holds TOO (owner, 2026-11-08; the favourite's card's rule since
// 2026-11-05): the chat stands in the pane and in the card at once. With favourites off, the rail stands only while the
// pane is shut, as it did. Beside the open pane (owner, 2026-11-05):
//   - NO GAP AT THE TOP: no glyph, and nothing keeps its room — the + sits
//     on the rail's own padding (claude-rail.css `.no-glyph`).
//   - THE + IS THE PANE'S HOME (`onNewChat`, claude-rail-lib `plusDoor`): the
//     pane's Recent chats page, the keyboard in its new-chat composer
//     (`paneHomeComposer`) — not the New task card, which is the shut pane's.
//   - A LIST RING'S PRESS PINS ITS CARD, AS SHUT (claude-rail-lib
//     `ringPress`; owner, 2026-11-08): the pane keeps its chat until the
//     card's Open door; a hover previews the card. (Until 2026-11-08 the
//     press opened the chat in the pane.) A favourite's ring is unchanged.
//   - THE PANE OPENING UNDER A PINNED LIST CARD closes it; the pane stands
//     on its home (2026-11-05 evening). The pane SHUTTING brings back no
//     list card (2026-11-06) — only the favourite whose Open expanded it
//     (window B, fav-peek-store `takeFavRestore`).
//   - THE LIST SCROLLS, every ring drawn, its scrollbar hidden (`.is-scroll`;
//     favourites on, shut pane too) — the clip of 2026-11-02 is the
//     favourites-off rail's.
//
// THE RAIL'S FOOT, 2026-11-05 (owner): the LIST is at most ten rings tall
// and scrolls, its edges fading where there is more (`listEdges`); a new
// place starts it at the top, and the active ring is scrolled into view
// without reordering anything. The BLOCKED chats (rail-peek-lib `favLike`,
// claude-rail-lib `favSeats`) stand at the head of the favourites' section
// as favourites in all but the star — temporary favourites. The starred
// stand newest star first.
// The PINNED BOTS (`pinnedBots`, platform/lib/botsPulse `useBotsRows`) stand
// under them, each a door to its page. The list stands beside the open pane
// on its HOME too (owner, 2026-11-06; `listHidden` answers never now: the
// same rings whether a chat is open or the pane is on Recent). The + is a page rail's only
// (`plusShown`). While the rail stands the document carries its width
// (`railUp`, `--claude-rail-w`) and the notification column and the shadcn
// menus keep off it (platform/lib/rail-gutter).
// ON A PAGE WITH NO RAIL (owner, 2026-11-05: home, Bots, Tasks, AI Models,
// Community, Preferences…): the shell's FALLBACK (FallbackRail, App.tsx), this
// rail with `favouritesOnly` — no place, no list, no Git, no glyph, but the +:
// the + (New task, on the default Fused folder) and the favourites. It stands only while no page rail
// is mounted (fav-peek-store `registerPageRail`, claude-rail-lib
// `fallbackRailUp`): every page rail counts itself from its layout phase.
//
// A SELECTED NODE SORTS THE RAIL (owner, 2026-11-10; it narrowed it from
// 2026-10-29): every chat on the canvas stays, the node's first, then a
// hairline, then the rest faded (claude-rail-lib `railRows`, `off`) — and the
// + makes a task ON the node — the card
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
// is the chat's door (`openChat`; the first such press included).
//
// FAVOURITES (owner, 2026-11-03): the card's STAR (RailPeek) makes a chat a
// favourite — server-side, global, so every rail lists the same ones (api
// `setTaskFavourite`; `starred` on the pulse); the rail's normal list leaves
// them out; the pane's Recent lists them (with their star) (owner, 2026-11-05). Which favourite's card
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
//     open — why?"): resting on it previews that chat in window B (hover
//     mode) when no favourite's card is open; with one open, every favourite
//     ring's hover is its caption alone (owner, 2026-11-04: "only the
//     tooltip with the title").
//     The two windows are independent: window A (the list's card) and window
//     B (the favourite's card, FavouritePeek). A fav ring's hover previews in
//     B and never touches A's pin; nothing in one section opens or closes
//     the other except star / unstar; A shrinks while B is open.
//     Its press opens the favourite's card and lets the hover card go — and
//     so does a press on that hover card's where-chip that leaves the page
//     (`onChipLeave`): the chat goes with the reader, in its own card.
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
// LIST ring shows that ring's title and moves nothing; its press swaps the
// chat. (A favourite's ring is the exception, above.)
// A pinned card can also be RESIZED from its left corners, and keeps that
// size for the next pin (RailPeek). With favourites off a hover card is
// always the default size; on, the hover card and the pinned one are ONE
// SIZE — the remembered one — and either resizes it (RailPeek `oneSize`).
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
// NO WAY-BACK SEAT (owner, 2026-11-10: "remove back to canvas button"): the
// canvas glyph that deselected the node (2026-11-01) is gone — with every
// chat on the canvas listed, there is no filter to get out of.
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type FocusEvent as ReactFocusEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { GitBranch, GitCommitHorizontal, Plus, RefreshCw, Search } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { getSchedule, markWholeTaskRead, showLauncher, type Task, type TaskPulseTask } from "@platform/lib/api";
import { useBotsAvailable, useLauncherAvailable } from "@platform/lib/bots-available";
import {
  onDraftChange,
  readListing,
  refreshListing,
  subscribeListing,
  useTasksPulse,
  useTasksPulseRows,
} from "@shell/tasksPulse";
import { BOTS_ICON, GIT_ICON, TASKS_ICON } from "@shell/nav-icons";
import { GIT_DOT_WORDS, gitBranch, gitDoorDots, gitDoorTitle, gitSyncWords } from "@shell/git-dock-lib";
import { pulseTitle } from "@shell/tasks-lib";
import { isBotsPath } from "@apps/bots/routes";
import {
  cardTitleLine,
  draftRing,
  isChatDraftTask,
  isDraftTask,
  liveJobRow,
  taskColumn,
  taskHref,
} from "@shell/tasks-lib";
import { StatusIcon } from "@shell/ScheduleTaskViews";
import NewJobModal, { chatHopSeed, reopenTime, type DraftSeed } from "@shell/NewJobModal";
import { fetchDrafts } from "@platform/lib/drafts";
import {
  clearFavOpen,
  favOpenedAtMs,
  botOpen,
  lastChatId,
  setBotOpen,
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
  dragging,
  favSeats,
  keepMarks,
  listHidden,
  listSeats,
  LIST_ROWS_MAX,
  nodeName,
  pinnedBots,
  holdBotOrder,
  botRingRoute,
  botStatusWords,
  plusShown,
  RAIL_WARM_MS,
  railRemembered,
  rememberRail,
  ringPress,
  placeName,
  placeRoot,
  queuedEditHref,
  railRows,
  railScope,
  railShown,
  readMark,
  ringMarks,
  type RailHere,
  type RailPlace,
  type RailRow,
  type ReadMark,
  windowAHovers,
} from "./claude-rail-lib";
import {
  FAV_SETTLE_MS,
  PEEK_MAX_WIDTH,
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  PeekIntent,
  clampAnchor,
  railCardSize,
  favLike,
  keepAlive,
  railOnChipLeave,
  railOnStarred,
  railOpenFav,
  rowOf,
  statusWords,
  placeClear,
  bottomClear,
  dragMaxWidth,
  snapWidth,
  type Box,
} from "./rail-peek-lib";
import { publishWindowABox, setSnapGuide } from "./card-snap";
import { useCanvasRailPeek, useRailFeatureOn } from "./rail-peek-flag";
import {
  dismissFavHover,
  favHoverIntent,
  focusRail,
  registerPageRail,
  registerRail,
  confirmFavRestore,
  requestFavFocus,
  setFavRestore,
  setRailPage,
  takeFavRestore,
  takeQuietFocus,
  useFavHoverKey,
  useFavPeekState,
} from "./fav-peek-store";
import { FAV_PEEK_ID } from "./FavouritePeek";
import { chatDraftKey, peekDraftSyncer } from "@platform/lib/drafts";
import { useRailGit } from "./rail-git";
import { publishRailGit } from "./rail-git-store";
import { PLUS_DOOR, PlusPopIntent } from "./plus-pop-lib";
import { publishPlusRender, usePlusCard, usePlusHosted, withdrawPlusRender } from "./plus-card-store";
import { useLauncherOpen } from "./launcher-open";
import { timeAgo } from "@platform/lib/format";
import { RailPeek, type RailPeekHandle } from "./RailPeek";
import { botHasUnread, botsPulseTitle, useBotsPulse, useBotsRows } from "@platform/lib/botsPulse";
import { navigateUrl } from "@platform/lib/router";
import { Face } from "@apps/bots/components/Face";
import type { FaceSubject } from "@apps/bots/lib/face";
import { BOT_PEEK_ID, botHref } from "./BotPeek";
import {
  botCardHandle,
  botCardIntent,
  botCardState,
  botPress,
  botPressed,
  botRing,
  clearBotRing,
  setBotCardHeld,
  setBotCardHoldsOnPin,
  setBotCardNoRoom,
  setBotRing,
  useBotCardState,
} from "./bot-card-store";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import {
  TASKS_CARD_KEY,
  TASKS_PEEK_ID,
  closeTasksCard,
  focusTasksCard,
  registerTasksDoor,
  tasksCardIntent,
  tasksDoorAction,
  useTasksCard,
} from "./tasks-peek-store";
import {
  BOTS_CARD_KEY,
  BOTS_PEEK_ID,
  botsCardIntent,
  botsDoorAction,
  closeBotsCard,
  focusBotsCard,
  registerBotsDoor,
  useBotsCard,
} from "./bots-peek-store";
import {
  GIT_CARD_KEY,
  GIT_PEEK_ID,
  closeGitCard,
  focusGitCard,
  forgetGitCardRail,
  setGitCardRail,
  gitCardIntent,
  gitDoorAction,
  registerGitDoor,
  useGitCard,
} from "./git-peek-store";

/** THE RAIL'S WIDTH, SAID TO THE DOCUMENT (owner, 2026-11-05: "move the
 *  notification modal to the right a little bit so it doesn't cover the mini
 *  sidebar … basically all menus will shift"): while a rail stands, the
 *  document's `--claude-rail-w` is its width, and every fixed surface on the
 *  window's right edge — the notification column (notifications.css), the
 *  shadcn menus and popovers (platform/lib/rail-gutter `railGutter`) — keeps
 *  off it. A hidden rail says nothing. Counted: a page's rail and the shell's
 *  fallback never stand together, but the count makes that a fact, not a hope. */
let railsUp = 0;
function railUp(): () => void {
  // A test renderer's document has no root element: nothing to say it to.
  const root = typeof document !== "undefined" ? document.documentElement : null;
  railsUp += 1;
  root?.style.setProperty("--claude-rail-w", "48px");
  return () => {
    railsUp -= 1;
    if (railsUp <= 0) {
      railsUp = 0;
      root?.style.removeProperty("--claude-rail-w");
    }
  };
}

/** The rail beside an open pane, for the pages' seam drags (rail-peek-lib
 *  `railInset`) — re-exported here, the one rail module apps may import. */
export { railInset } from "./rail-peek-lib";
/** The pane's home on a page's URL, for the pages' `onNewChat` (claude-rail-
 *  lib `paneHomeHref`) — re-exported for the same reason. */
export { paneHomeHref } from "./claude-rail-lib";


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

/** THE WAIT FOR A CARD'S COMPOSER (`focusComposerSoon`, the bot card's too):
 *  retried by frame until the composer exists and took the keyboard, for
 *  FOCUS_WAIT_MS at most; over the moment the card no longer shows the chat
 *  (`still`), the reader moves the keyboard to a control outside the card
 *  (not the body, not the pressed ring), or does anything themselves — any
 *  later press, and any later key when `keys` (capture, document-wide).
 *  Returns the stop. */
function composerWaitFor(o: {
  still: () => boolean;
  cardId: string;
  focus: () => boolean;
  ring: () => HTMLElement | null;
  keys: boolean;
  onStop: () => void;
}): () => void {
  let live = true;
  let raf = 0;
  const stop = () => {
    live = false;
    cancelAnimationFrame(raf);
    document.removeEventListener("pointerdown", stop, true);
    document.removeEventListener("keydown", stop, true);
    o.onStop();
  };
  document.addEventListener("pointerdown", stop, true);
  if (o.keys) document.addEventListener("keydown", stop, true);
  const until = performance.now() + FOCUS_WAIT_MS;
  const attempt = () => {
    if (!live) return;
    if (!o.still()) return stop();
    const card = document.getElementById(o.cardId);
    const a = document.activeElement;
    if (a && a !== document.body && !card?.contains(a) && a !== o.ring()) return stop();
    if (o.focus() && card?.contains(document.activeElement)) return stop();
    if (performance.now() < until) raf = requestAnimationFrame(attempt);
    else stop();
  };
  raf = requestAnimationFrame(attempt);
  return stop;
}

/** Focus into a card once it is drawn (`focusCardSoon`): the class lands a
 *  frame after the show, and a focus on a `visibility: hidden` box does
 *  nothing, so try for a few frames rather than guess the one. */
function cardFocusSoon(cardId: string, focus: () => boolean): void {
  let tries = 0;
  const attempt = () => {
    const card = document.getElementById(cardId);
    if (focus() && card?.contains(document.activeElement)) return;
    if (++tries < 12) requestAnimationFrame(attempt);
  };
  requestAnimationFrame(attempt);
}

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
  /** THE +'s CARD AS A POPOVER (owner, 2026-11-07, the rail flag — `navDoors`):
   *  floating beside the rail, no scrim, like the rail's chat card. Only the
   *  +'s; a draft's card stays the modal. */
  pop?: boolean;
  /** …PINNED, or a hover's preview (owner, 2026-11-07: "hover gives you quick
   *  preview", a click cements it; plus-pop-lib.ts). A pinned card closes by
   *  its × only. */
  pinned?: boolean;
  /** Which popover door opened it (plus-pop-lib): `PLUS_DOOR`, or the draft
   *  ring's key. */
  door?: string;
  /** THE TASK JUST CREATED from this card (NewJobModal `onCreatedChat`): the
   *  remounted card opens on that entry's chat instead of closing. */
  chatEntry?: string;
  /** …and the settings it was created with, for that chat until its row is
   *  listed (CodeRabbit on #2220). */
  chatSeed?: { model?: string; effort?: string; backend?: string };
}

/** A draft's fresh record read for its New task card (`readDraftCard`):
 *  the card to open, or the record GONE, or NO ANSWER. */
type DraftRead = { kind: "card"; card: Omit<Card, "seq"> } | { kind: "gone" } | { kind: "unknown" };

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
  return (row && isDraftTask(row) ? draftHref(row) : null) ?? taskHref(merged) ?? queuedEditHref(row, t.status);
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

/** The foot's pinned bots, in the order first drawn this page load
 *  (claude-rail-lib `holdOrder`). */
let heldBotOrder: string[] = [];

/** IS THE +'s CARD MID-CREATE? (Modal's `data-busy`; CodeRabbit on #2220.)
 *  Read off the card, not its ✕ — the card hides its ✕ while it shows a chat,
 *  and an idle card must still give way to the Tasks or Bots card. True with
 *  no card standing, as the missing ✕ used to read. */
function popBusy(rail: HTMLElement | null): boolean {
  const card = rail?.ownerDocument.querySelector(".modal-popover .modal-dialog");
  return !card || card.hasAttribute("data-busy");
}


/** The + popover's box as window A's (card-snap.ts) — stable, so the Modal's
 *  publish effect runs only when the box moves. */
function publishPlusBox(box: Box | null): void {
  publishWindowABox("plus", box);
}

export function ClaudeRail({
  place,
  newTask,
  git,
  activeSessionId,
  onOpen,
  onOpenChat,
  onSelectNode,
  onTagSelect,
  onPullStart,
  onCardPinned,
  paneOpen = false,
  onNewChat,
  favouritesOnly = false,
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
   *  panel (the explorer's git companion, the app page's git peek).
   *  With the rail flag (owner, 2026-11-10) the section is one Git door
   *  under the rings whose window A card (GitPeek) holds all of it. */
  git?: { path: string; onOpenGit?: () => void };
  /** The session the page's URL names, drawn as the current row. */
  activeSessionId: string | null;
  /** Open the pane as it is. */
  onOpen: () => void;
  /** Open one of the canvas's chats: its Tasks-page door (`taskHref`). */
  /** …answering, when it can, whether the hop went: a page door behind the
   *  composer's leave guard resolves false when the reader stayed
   *  (CanvasPage / AppPage `openChatHere`, `navigateUrl`). The favourite
   *  card's Open waits for that answer before closing (FavouritePeek). */
  onOpenChat: (href: string) => void | Promise<boolean>;
  /** The card's node chip: select that node in the workbench (RailPeek);
   *  canvas pages only — without it the chip is a label and the foot's
   *  canvas seat is not drawn. Null — the rail's last seat while a node is
   *  selected (header) — asks
   *  it to deselect, back to the whole canvas. */
  onSelectNode?: (udfName: string | null) => void;
  /** The cards' place tags' press (claude-rail-lib `RailHere.tag`): the
   *  node selected and zoomed to, or the canvas fit — the pane's chat
   *  untouched (CanvasWorkspace `selectFromTag`). */
  onTagSelect?: (udfName: string | null, sessionId?: string | null) => void;
  /** A pointer down on the rail's inner edge: the start of a pull that opens
   *  the pane at the width the pointer drags it to (CanvasWorkspace
   *  `onRailPull`). */
  onPullStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
  /** A card opened by a press is up (`true`) or gone (`false`): the page
   *  holds its frame's focus-on-enter meanwhile (header). */
  onCardPinned?: (pinned: boolean) => void;
  /** THE PANE IS OPEN beside this rail (header, "BESIDE THE OPEN PANE"):
   *  favourites on only — the page mounts the rail then, at the window's
   *  right edge past the pane. No panel glyph and no pull seam (the pane's
   *  own Hide and seam do those jobs), and the rail hides while it has
   *  nothing to draw (claude-rail-lib `railShown`). */
  paneOpen?: boolean;
  /** THE PANE'S HOME (header, "BESIDE THE OPEN PANE"): open the pane on its
   *  Recent chats page, a new chat's composer — what the + does while the
   *  pane is open (claude-rail-lib `plusDoor`); the rail then puts the
   *  keyboard in that composer. Absent: the + opens the New task card. */
  onNewChat?: () => void;
  /** THE SHELL'S FALLBACK RAIL (FallbackRail; header, "ON A PAGE WITH NO
   *  RAIL"): no place of its own — no list, no +, no Git, no panel glyph;
   *  only the favourites, and hidden while there are none. */
  favouritesOnly?: boolean;
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
  const favsOn = useRailFeatureOn();
  const favsOnRef = useRef(favsOn);
  favsOnRef.current = favsOn;
  const favs = useMemo(() => (favsOn ? favSeats(rows, full) : []), [favsOn, rows, full]);
  // THE BLOCKED ARE AT THEIR HEAD (claude-rail-lib `favSeats`, rail-peek-lib
  // `favLike`; owner, 2026-11-05): favourites in every way bar the star —
  // window B's hover, the favourite's card, the pane hand-over — drawn with
  // `.is-blocked` and no star. How many, for the rail's standing (`railShown`:
  // the fallback never stands for them alone).
  const blockedCount = useMemo(() => favs.filter((s) => !s.pulse.starred).length, [favs]);
  // THE PINNED BOTS, UNDER THE FAVOURITES (claude-rail-lib `pinnedBots`;
  // owner, 2026-11-05): their faces, each a door to the bot's page.
  // Only where the server offers Bots (`botsAvailable`, the left sidebar's
  // own gate) — no pulse poll on Windows — and the config is read only when
  // something here would use the answer (favourites or the nav doors).
  const botsOffered = useBotsAvailable(favsOn || peekOn);
  const botsOn = favsOn && botsOffered;
  const botRows = useBotsRows(botsOn);
  // …IN AN ORDER SET ONCE PER PAGE LOAD (claude-rail-lib `holdOrder`; owner,
  // 2026-11-09): a press on a bot no longer reshuffles the foot. Held at the
  // module, not the mount, so a view switch (a new page rail) keeps it too;
  // dropped while the foot is off, so turning it back on seeds afresh
  // (`holdBotOrder`).
  const bots = useMemo(() => {
    const sorted = pinnedBots(botRows, { footOrder: peekOn });
    const held = holdBotOrder(heldBotOrder, sorted, botsOn && peekOn);
    heldBotOrder = held.held;
    // STACKED FROM THE BOTTOM (owner, 2026-11-09: "it should be reverse —
    // 1st pin bottom right, 2nd pin 2nd to the bottom"): the Bots page's
    // first is the rail's last seat, nearest the window's corner.
    return peekOn ? [...held.out].reverse() : held.out;
  }, [botRows, peekOn, botsOn]);
  const allSeats = useMemo(
    () => (favouritesOnly ? [] : listSeats(railRows(rows, full, seatPlace), favsOn)),
    [rows, full, seatPlace, favsOn, favouritesOnly],
  );
  // THE LIST STOOD ASIDE FOR THE PANE'S HOME until 2026-11-06 (claude-rail-
  // lib `listHidden`, never true now): the rings stand whatever the pane
  // shows. The pane is home when the page's URL names no session
  // (`activeSessionId` — every page's pane routes by `session_id`: present,
  // that chat; absent, the home page).
  const paneHome = !activeSessionId;
  // The +'s New task card (`openNewTask`, below) — declared up here because
  // an open one keeps the rail standing (`shown`).
  // THE CARD'S STATE IS THE SHELL'S (plus-card-store): a pinned card
  // outlives this rail's page, as the Tasks card does.
  const [card, setCard] = usePlusCard<Card>();
  const cardRef = useRef(card);
  cardRef.current = card;
  // WHETHER THE RAIL STANDS (header, claude-rail-lib `railShown`): with the
  // pane shut, always; beside the open pane (favourites on), only while it
  // has a chat or a favourite to draw — or the +'s card is up, which must
  // not vanish under the reader. HIDDEN, not unmounted: the rail's
  // subscriptions and measures stay put, and it is back the moment a chat
  // lands. A hidden rail does not register (below), so the favourite's card
  // has no rail to stand beside — there is no favourite to show then anyway.
  // Beside an open pane the rail may pop in or out as the place's first chat
  // lands or its last one leaves (accepted, owner 2026-11-05).
  // THE PANEL GLYPH AND THE PULL SEAM: only while the pane is shut, and
  // never on the shell's fallback (no pane to show).
  const chrome = !paneOpen && !favouritesOnly;
  // THE RAIL'S NAV DOORS (owner, 2026-11-07; behind the rail flag
  // `canvas_rail_peek_enabled` — `peekOn`): Tasks and the + at the top, in
  // place of the panel glyph, and Bots at the foot — the left sidebar drops
  // its own Tasks and Bots rows (GlobalSidebar). They are on every rail,
  // always: the rail stands whatever it holds and whatever the pane does
  // (the shell's fallback stands on every page without a page rail,
  // claude-rail-lib `fallbackRailUp`). Off, the rail of 2026-11-06.
  const navDoors = peekOn;
  // THE GIT SECTION BECOMES A DOOR with the nav doors (owner, 2026-11-10:
  // "move Git to a door on the rail, under the task rings"): the rail reads
  // the page's repository, draws the Git door (below the rings, past a
  // hairline) and hands the facts, with the page's Git panel door, to the
  // shell's Git card (rail-git-store.ts, keyed by this rail; GitPeek).
  const openGitRef = useRef(git?.onOpenGit);
  openGitRef.current = git?.onOpenGit;
  const gitOn = !!git;
  const gitDoor = !!git?.onOpenGit;
  const gitRepo = navDoors && gitOn ? (railGit.repo ?? null) : null;
  const hideList = listHidden({ paneOpen, favsOn, paneHome });
  const stands =
    navDoors ||
    railShown({
      list: hideList ? 0 : allSeats.length,
      favs: favs.length - blockedCount,
      blocked: blockedCount,
      bots: bots.length,
      paneOpen,
      always: favsOn,
      favouritesOnly,
    }) || card !== null;
  // STANDING FROM THE FIRST PAINT after a reload (claude-rail-lib
  // `railRemembered`): a rail that stood in this window last stands again
  // at once, empty, until the pulse and the bots say what it holds — or,
  // past `RAIL_WARM_MS`, that it holds nothing. The flag itself is
  // remembered the same way (favourites-flag), or the fallback could not
  // mount at all.
  const [warming, setWarming] = useState(() => favsOn && railRemembered());
  useEffect(() => {
    if (!warming) return;
    const t = window.setTimeout(() => setWarming(false), RAIL_WARM_MS);
    return () => window.clearTimeout(t);
  }, [warming]);
  useEffect(() => {
    if (stands) {
      setWarming(false);
      rememberRail(true);
    } else if (!warming) rememberRail(false);
  }, [stands, warming]);
  const shown = stands || warming;
  // ONE CHAT, ONE COMPOSER beside the open pane: a chat the pane (or the
  // explorer's sidebar) has mounted is not mounted again in this rail's card
  // — the card says where it is instead (`bodyNote`), as the favourite's card
  // does (FavouritePeek). Only while the pane is open: shut, nothing is
  // mounted there, and the rail is the rail of 2026-11-04.
  // WITH FAVOURITES ON, THE LIST SCROLLS (owner, 2026-11-05: "remove the
  // clip-not-scroll restriction"): every ring is drawn and the list scrolls
  // in its share of the column, its scrollbar hidden like the favourites'
  // (claude-rail.css `.is-scroll`); the favourites keep their floor. With
  // favourites off it is the rail of 2026-11-02, below.
  // ONLY AS MANY RINGS AS FIT, from the top — no scroll (owner, 2026-11-02:
  // "if we have more chats let's not add scroll, just clip them; basically we
  // only show the top chats"). The list is its rings' height and the foot
  // stands right under the last ring (owner, 2026-11-03: "the back to canvas
  // button should be end of list"); the column's leftover is the ROOM, an
  // empty box under the Git section (claude-rail.css `.claude-rail-room`). How many
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
    if (!el || favsOn) return;
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
  }, [anyRing, favsOn]);
  const baseSeats = useMemo(
    () => (!favsOn && fit < allSeats.length ? allSeats.slice(0, fit) : allSeats),
    [allSeats, fit, favsOn],
  );
  // THE LIST'S EDGES (owner, 2026-11-05: "subtle indicators showing this is
  // not the top and this is not the bottom"): whether there is more above
  // and below the scrolled box — read on every scroll and whenever the
  // rings or the box change; the fades draw off it (claude-rail.css).
  const [listEdges, setListEdges] = useState({ up: false, down: false });
  const measureEdges = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    const up = el.scrollTop > 1;
    const down = el.scrollTop + el.clientHeight < el.scrollHeight - 1;
    setListEdges((e) => (e.up === up && e.down === down ? e : { up, down }));
  }, []);
  useLayoutEffect(() => {
    measureEdges();
    const el = listRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measureEdges);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measureEdges, baseSeats, hideList]);

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
  // NOT A PLACE CHANGE: the canvas `place` is remade with the node's file,
  // but it is the same canvas — compared by `placeRoot`, which leaves the
  // node out. (From 2026-11-03 to 2026-11-06 a held card whose chat was not
  // the node's stayed, its ring riding into the foot above the canvas glyph
  // — `heldExtra`. Gone: see below.)
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
  // THE HELD CARD GOES WHEN ITS RING LEAVES THE LIST (owner, 2026-11-06:
  // "when you have a click preview open and the tasks in that list get
  // filtered because of selection and the task is no longer in the list, we
  // close the window"): a node selected narrows the list to its chats, and a
  // card pinned on a chat that is not among them closes — no outsider's ring
  // in the foot any more. In the layout phase, with the list. Favourites
  // off, a blocked chat's held card keeps its ring in the favourites' box.
  const heldKey = held ? peekKey : null;
  useLayoutEffect(() => {
    if (!heldKey) return;
    if (baseSeats.some((s) => s.pulse.key === heldKey)) return;
    if (!favsOn && favs.some((s) => s.pulse.key === heldKey)) return;
    setHeld(false);
    intent.dismiss();
  }, [heldKey, baseSeats, favs, favsOn, intent]);
  // Every seat this rail's card may show — the list's, plus the favourites
  // (a hover on their rings, header) — for the card's anchors, the kept
  // mounts and the prune; the LIST itself is `baseSeats`, the favourites
  // draw in their own section (below). A favourite's OPEN card is the
  // shell's (FavouritePeek), never this one.
  const seats = useMemo(() => (favs.length > 0 ? [...baseSeats, ...favs] : baseSeats), [baseSeats, favs]);
  const favKeys = useMemo(() => new Set(favs.map((s) => s.pulse.key)), [favs]);
  // Read at the moment, not at the render that made the callback: the chip's
  // hand-over runs after the hop (`onChipLeave`).
  const favKeysRef = useRef(favKeys);
  favKeysRef.current = favKeys;
  const seatsRef = useRef(seats);
  seatsRef.current = seats;
  // THE LAST RING PRESSED (owner, 2026-11-02): "the active css to the last
  // selected task… keep by task id so it is easier to spot; it doesn't
  // change when we filter tasks based on the selected node, only when we
  // click a task item". A press on a ring, either door; nothing else moves
  // it, and it survives the rail narrowing to a node and widening again.
  // Shared with the pane's Recent chats list (platform/lib/last-chat), so
  // the mark holds across the pane opening and shutting. THE FAVOURITES'
  // SECTION KEEPS ITS OWN (header, `useLastFav`).
  const lastPressed = useLastChat();
  const lastFavPressed = useLastFav();
  const railRef = useRef<HTMLElement | null>(null);
  const peekRef = useRef<RailPeekHandle | null>(null);
  const ringEls = useRef(new Map<string, HTMLElement>());
  // WINDOW A STANDS AT THE LIST'S TOP (owner, 2026-11-06: "window A should
  // be aligned with the top of the list's first item, no matter whether the
  // selected item is below, above or out of the list"): every list ring's
  // card anchors on the FIRST ring of the list — RailPeek clamps that to the
  // list's scrolled box, so the card's top is the box's top — while the safe
  // cone still starts at the ring the pointer is on (`coneOf`).
  const anchorOf = useCallback((key: string) => {
    const ring = ringEls.current.get(key) ?? null;
    if (!ring) return null;
    // Favourites on, and a ring OF THE LIST (favourites off, the blocked
    // box's rings are window A's too, and anchor on themselves as before).
    const list = listRef.current;
    if (!favsOnRef.current || !list?.contains(ring)) return ring;
    return list.querySelector<HTMLElement>("a[data-task-key]") ?? ring;
  }, []);
  const coneOf = useCallback((key: string) => ringEls.current.get(key) ?? null, []);
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
  // WINDOW B'S HOVER (fav-peek-store): the favourite whose hover card is up.
  const favHoverKey = useFavHoverKey();
  // THE PAGE'S CANVAS, for both cards' where-chips (claude-rail-lib
  // `chipDoor`): on the canvas page a chip naming this canvas selects in
  // place. A getter over a ref: the favourite's card reads it at its press
  // (fav-peek-store `railHere`), and the selected node moves often.
  const hereRef = useRef<RailHere | null>(null);
  hereRef.current =
    place.kind === "canvas" && onSelectNode
      ? {
          env: place.env,
          name: place.name,
          file: place.file ?? null,
          select: onSelectNode,
          ...(onTagSelect ? { tag: onTagSelect } : {}),
        }
      : null;
  const hereOf = useCallback(() => hereRef.current, []);
  // A PAGE'S RAIL COUNTS while mounted, shown or not (fav-peek-store
  // `registerPageRail`): the shell's fallback stands only while none does.
  // In the layout phase, so a hop's swap lands before it paints.
  useLayoutEffect(() => (favouritesOnly ? undefined : registerPageRail()), [favouritesOnly]);
  // In the layout phase too: the fallback and a page's rail swap places in
  // one commit (header), and the favourite's card must never see neither.
  useLayoutEffect(() => {
    const el = railRef.current;
    // …and its chat door, for the favourite card's Open (fav-peek-store
    // `railDoor`): the PAGE's pane door, not a bare navigation — and not
    // this rail's `openChat`, whose last-picked mark is the list's (a
    // favourite's door moves the favourites' mark, FavouritePeek). A
    // favourite always has a conversation (only a started chat can be
    // starred; a blocked one is running), so no draft card is in question.
    return el && shown ? registerRail(el, hereOf, (href) => onOpenChatRef.current(href)) : undefined;
  }, [hereOf, shown]);
  // THE GIT DOOR'S FACTS, for the shell's Git card (rail-git-store, by this
  // rail). In the layout phase, with the rail's own registration just above:
  // a hop's next rail says "reading" in the same commit it stands up in, so
  // a pinned Git card following the reader never sees a rail without git.
  const gitReading = navDoors && gitOn && railGit.repo === undefined;
  useLayoutEffect(() => {
    const el = railRef.current;
    if (!el) return;
    publishRailGit(
      el,
      gitRepo
        ? {
            repo: gitRepo,
            checking: railGit.checking,
            checked: railGit.checked,
            check: railGit.check,
            ...(gitDoor ? { onOpenGit: () => openGitRef.current?.() } : {}),
          }
        : gitReading
          ? "reading"
          : null,
    );
  }, [gitRepo, gitReading, gitDoor, railGit.checking, railGit.checked, railGit.check]);
  useLayoutEffect(() => {
    const el = railRef.current;
    return el
      ? () => {
          publishRailGit(el, null);
          // The Git card this rail opened: a preview goes, a pinned one
          // follows to the top rail (git-peek-store `forgetGitCardRail`).
          forgetGitCardRail(el);
        }
      : undefined;
  }, []);
  // Favourites on only (flag-off parity: the rail of 2026-11-04 moved
  // nothing).
  useLayoutEffect(() => (shown && favsOn ? railUp() : undefined), [shown, favsOn]);
  // …and WHERE THE PAGE STANDS, for the favourite card's where-chip (fav-
  // peek-store `setRailPage`): said again on every hop this rail makes in
  // place and every node selected. After the register above, in the same
  // commit, so the card hears one change.
  const pageNode = place.kind === "canvas" ? (place.file ?? null) : null;
  useLayoutEffect(() => {
    const el = railRef.current;
    // The fallback stands on no page (header): no place for a chip to name.
    if (el && shown) setRailPage(el, favouritesOnly ? null : { root, node: pageNode });
  }, [root, pageNode, shown, favouritesOnly]);
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
  // …AND A CHAT STARRED ANYWHERE while this card shows it (another window's
  // star): the list no longer holds it (header, `listSeats`), so this card
  // lets go and the ring is the favourites section's. Only on the TURN to
  // starred while shown: a favourite's ring now opens this card on a hover
  // (header), and that card must stay. In the layout phase, while it is
  // still a seat, so `letGo` finds its draft to flush.
  // A star pressed on in this rail's card: the ring moved to the favourites
  // section, so this card lets go now, and the favourite's card opens in
  // this window — a star you just pressed is a card you want to see
  // (rail-peek-lib `railOnStarred`).
  // A STAR PRESSED IN WINDOW A MOVES NOTHING (owner, 2026-11-05: "the item
  // also stays in the normal tasks section and the modal also stays; only
  // one more item is added in the fav section"): the card stays, the list
  // ring stays, and the favourites gain a ring — nothing hands over.
  const onStarred = useMemo(() => railOnStarred(() => false, letGo, setFavOpen), [letGo]);
  // A FAVOURITE'S RING PRESSED (header): this window's favourite card opens
  // on it at once — a local write, nothing to wait for — and this rail's card
  // lets go of the chat first. A key that is not a live favourite (unstarred
  // a moment ago elsewhere, archived) opens no card: the ring's door instead.
  // A keyboard press asked for the favourite card's × (fav-peek-store
  // `pendingFocus`).
  const openFavourite = useCallback(
    (t: TaskPulseTask, href: string) => {
      // A favourite as the rail counts one — starred or blocked (rail-peek-
      // lib `favLike`); anything else goes through the chat's door.
      if (!favLike(t) || t.status === "archived" || !t.session_id) {
        requestFavFocus(null);
        openChatRef.current(href);
        return;
      }
      // Window A keeps whatever it shows (owner, 2026-11-05: the sections
      // do not influence each other).
      setFavOpen(t.key);
    },
    [],
  );
  // A FAVOURITE'S CHAT LEFT BY THIS CARD'S WHERE-CHIP (owner, 2026-11-05: a
  // press "navigates to that canvas page but does not open the favourite
  // card; a second press opens it"): the hover card on a favourite's ring is
  // this rail's, and goes with the page — so as the chip's hop goes (once
  // the router's leave guard let it: RailPeek, rail-peek-lib `onHop`), the
  // chat is handed to the favourite's card in this window (the open key is
  // sessionStorage: it survives the hop, and the card stands on the next
  // page's rail). The same hand-over as the ring's press (`openFavourite`),
  // a keyboard press asking for the card's focus the same way.
  const onChipLeave = useMemo(() => {
    const seatOf = (key: string) => seatsRef.current.find((s) => s.pulse.key === key);
    return railOnChipLeave(
      () => favsOnRef.current,
      // The refs, not this render's sets: the hand-over runs once the hop
      // lands, and a chat starred in between is a favourite by then.
      // Never a favourite as far as window A is concerned (owner,
      // 2026-11-05): the sections do not hand over to each other.
      (_key) => false,
      letGo,
      (key, keyboard) => {
        if (keyboard) requestFavFocus(key);
        const seat = seatOf(key);
        if (seat) setLastFav(lastChatId(seat.pulse));
        setFavOpen(key);
      },
    );
  }, [letGo]);
  // JSON, not a joined string: a key is free to contain any delimiter.
  const seatKeys = JSON.stringify(seats.map((s) => s.pulse.key));
  useEffect(() => {
    const live = new Set(JSON.parse(seatKeys) as string[]);
    intent.prune(live);
    setKept((k) => (k.every((key) => live.has(key)) ? k : k.filter((key) => live.has(key))));
  }, [seatKeys, intent]);
  // …the pane's own chat included (owner, 2026-11-08: "why is the same task
  // blank in window A when the sidebar shows it, while window B works? it
  // should work in window A as well"): the chat stands in the pane and in
  // the card at once, the favourite's card's rule of 2026-11-05. The
  // one-chat-one-composer exclusion (`paneSids`) is retired.
  const keptSeats = useMemo(
    () => (peekOn ? kept.map((k) => seats.find((s) => s.pulse.key === k)).filter((s): s is NonNullable<typeof s> => !!s) : []),
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
  // press answers with no room from then on: it follows the chat's door
  // (`openChat`; `pressedHref`, set on the press, read during the commit it causes —
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
  const focusCardSoon = useCallback(() => cardFocusSoon(PEEK_ID, () => !!peekRef.current?.focusChat()), []);
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
      // Focus gone to a control outside the card (not the body, not the
      // pressed ring): the reader moved on, never pull it back (CodeRabbit
      // on #2037) — `composerWaitFor`'s own rule.
      const stop: () => void = composerWaitFor({
        still: () => intent.current === key,
        cardId: PEEK_ID,
        focus: () => !!peekRef.current?.focusComposer(),
        ring: () => ringEls.current.get(key) ?? null,
        keys,
        onStop: () => {
          if (composerWait.current === stop) composerWait.current = null;
        },
      });
      composerWait.current = stop;
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
  // THE BOT CARD (BotPeek.tsx; owner, 2026-11-05: "a small overlay that
  // just displays the chat exchange of the bot — the same exact behavior as
  // the task floating box"): the pinned bots' rings (the rail's last section,
  // `bots`) open a card of their own, on their own intent machine
  // (`botIntent`, the list card's PeekIntent): a hover previews the bot's
  // chat, a press pins it (held, favourites on — the only way the bots are
  // drawn), the × / Escape / an outside press close it, Open is the door to
  // the bot's page. ON THE BOTS PAGE the ring is that door again
  // (claude-rail-lib `botRingRoute`): the page shows the bot itself, and a
  // card up when the reader lands there closes. The rules below are the
  // list card's, one each (the outside press, Escape with the focus nowhere,
  // the press into the workbench frame), on the bot card's own state.
  // THE CARD IS THE SHELL'S (bot-card-store, BotCardHost; owner, 2026-11-08:
  // a view switch remounts this rail, and a card that lived here flickered
  // out and back): the state and the intent machine are the store's, the
  // rings this rail's own (registered by id for the card to stand level with).
  const botCard = useBotCardState();
  const botKey = botCard.key;
  const botPinned = botCard.pinned;
  const botHeld = botCard.held;
  const setBotHeld = setBotCardHeld;
  const botHeldRef = useRef(false);
  botHeldRef.current = botHeld;
  // A pin holds the card where favourites are on (the list card's rule).
  setBotCardHoldsOnPin(favsOn);
  const botIntent = botCardIntent();
  // This rail's own rings, for the keyboard's way back; the store has them
  // too, by id, for whichever rail drew them last.
  const botEls = useRef(new Map<string, HTMLElement>());
  // A bot unpinned or hidden under the pointer takes its card with it.
  const botIds = JSON.stringify(bots.map((b) => b.id));
  // THE OUTGOING RAIL LETS GO (CodeRabbit on #2201): a page switch unmounts
  // this rail's rings with no leave event, so the shared machine would still
  // think the pointer or the focus rests on one — a preview stuck up, or a
  // pending one opening after the hop. A pinned card is left alone (the
  // leave only ever starts a preview's close).
  useEffect(
    () => () => {
      if (botCardState().pinned) return;
      botIntent.leaveRing("pointer");
      botIntent.leaveRing("focus");
    },
    [botIntent],
  );
  // Only once this rail has the bots to read (a rail mounting before the
  // config answered draws none, and must not prune a card the shell holds).
  const botsKnown = favsOn && botsOffered;
  useEffect(() => {
    if (!botsKnown) return;
    botIntent.prune(new Set(JSON.parse(botIds) as string[]));
  }, [botsKnown, botIds, botIntent]);
  // ONE WINDOW B (owner, 2026-11-06: "treat fav bots and fav tasks as one
  // window — no resize, no jitter, no overlap"): the bot's card and the
  // favourite's (FavouritePeek) stand in the same place at the same size,
  // and one is up at a time. A pinned bot's card says so to the shell
  // (fav-peek-store `setBotCard`: the favourite's card shows no hover, the
  // favourites' rings hover nothing); a favourite OPENING — a ring's press,
  // a star, a chip hand-over, the pane's collapse — closes the bot's card;
  // and the two HOVERS close each other (a favourite's hover up while a
  // bot's is, or the other way round, would be two cards in one spot).
  const botUp = peekOn && botKey !== null && botPinned ? botKey : null;
  // The favourite as THIS RAIL counts it open (`openFavKey`, rail-peek-lib
  // `railOpenFav`), not the raw window key: a key with no card standing
  // (unstarred elsewhere, its verdict pending) must not touch the bot's.
  useEffect(() => {
    if (openFavKey && botKey) {
      setBotHeld(false);
      botIntent.dismiss();
    }
  }, [openFavKey, botKey, botIntent]);
  useEffect(() => {
    if (!favHoverKey) return;
    if (botKey && !botPinned) botIntent.dismiss();
  }, [favHoverKey, botKey, botPinned, botIntent]);
  useEffect(() => {
    if (botKey) dismissFavHover();
  }, [botKey]);
  // The flag off with the card up: nothing to show. (The Bots page is no
  // longer a reason — owner, 2026-11-06: the card stands there too, and only
  // its Open changes the page.)
  useEffect(() => {
    if (!peekOn && botKey) {
      setBotHeld(false);
      botIntent.dismiss();
    }
  }, [peekOn, botKey, botIntent]);
  // THE OUTSIDE PRESS (the list card's rule above, on this card): a pinned
  // card exempts the bots' own rings and the bridge; a hover card the whole
  // rail; a held card nothing closes but its × and Open.
  useEffect(() => {
    if (!botKey) return;
    let tick = 0;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      const el = t instanceof Element ? t : null;
      if (el?.closest(".claude-rail-peek-bridge")) return;
      if (botHeldRef.current) return;
      if (botPinned) {
        if (el && railRef.current?.querySelector(".claude-rail-bots")?.contains(el)) return;
      } else if (railRef.current?.contains(t)) return;
      botPress.inside = false;
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        if (!botPress.inside) botIntent.dismiss();
      }, 0);
    };
    const onUp = () => botIntent.pressEnd();
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
    return () => {
      window.clearTimeout(tick);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
    };
  }, [botKey, botIntent, botPinned]);
  // NO ROOM (the list card's `onNoRoom`): the ring is a door until a resize,
  // and a press that found no room opens the bot's page.
  const botNoRoom = botCard.noRoom;
  const setBotNoRoom = setBotCardNoRoom;
  // …until the window resizes, OR THE ROW GROWS (CodeRabbit on #2143): the
  // sidebar folding widens the row with no window resize, and the card may
  // fit then. The observer fires once as it starts (the width as it is), so
  // only a width PAST that one clears the flag.
  useEffect(() => {
    if (!botNoRoom) return;
    const re = () => setBotNoRoom(false);
    window.addEventListener("resize", re);
    const row = rowOf(railRef.current);
    let was = row?.getBoundingClientRect().width ?? 0;
    const ro =
      row && typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => {
            const w = row.getBoundingClientRect().width;
            if (w > was + 1) re();
            was = Math.max(was, w);
          })
        : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [botNoRoom]);
  useEffect(() => {
    setBotNoRoom(false);
  }, [favBox]);
  const dismissBotToRing = useCallback(() => {
    const key = botIntent.current;
    botIntent.dismiss(true);
    if (key) (botEls.current.get(key) ?? botRing(key))?.focus({ preventScroll: true });
  }, [botIntent]);
  const botComposerWait = useRef<(() => void) | null>(null);
  const focusBotComposerSoon = useCallback(
    (id: string) => {
      botComposerWait.current?.();
      const stop: () => void = composerWaitFor({
        still: () => botIntent.current === id,
        cardId: BOT_PEEK_ID,
        focus: () => !!botCardHandle()?.focusComposer(),
        ring: () => botEls.current.get(id) ?? botRing(id),
        keys: true,
        onStop: () => {
          if (botComposerWait.current === stop) botComposerWait.current = null;
        },
      });
      botComposerWait.current = stop;
    },
    [botIntent],
  );
  useEffect(() => () => botComposerWait.current?.(), []);
  // A ring's press: the card, shown now and pinned, the keyboard on its way
  // to the composer (the list card's `pressRing`, the card route alone).
  // ONE WINDOW B (header): a bot's card taking it — the open favourite
  // closes, its hover goes, and a restore a door left is forgotten. The
  // press and the keyboard's ArrowLeft alike (reviewer: the open-favourite
  // effect above would dismiss a card that took nothing first).
  const takeWindowB = () => {
    setFavOpen(null);
    dismissFavHover();
    setFavRestore(null);
  };
  const pressBotRing = (id: string, href: string) => {
    // A TOGGLE (owner, 2026-11-06: "a task item on the rail is also a toggle
    // to open and close the window"): the ring of the bot whose card a press
    // pinned closes it.
    if (botPinned && botKey === id) {
      hideHint();
      setBotHeld(false);
      botIntent.dismiss();
      return;
    }
    botPressed.href = href;
    requestAnimationFrame(() => {
      botPressed.href = null;
    });
    takeWindowB();
    botIntent.press(id);
    hideHint();
    focusBotComposerSoon(id);
  };
  // ESCAPE WITH THE FOCUS NOWHERE closes a pinned bot card (the list card's
  // rule).
  useEffect(() => {
    if (!botPinned || !botKey) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target !== document.body && e.target !== document.documentElement) return;
      if (botHeldRef.current) return;
      e.preventDefault();
      dismissBotToRing();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [botPinned, botKey, dismissBotToRing]);
  const onBotRingKey = (id: string) => (e: ReactKeyboardEvent<HTMLAnchorElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      takeWindowB();
      botIntent.showNow(id, true);
      cardFocusSoon(BOT_PEEK_ID, () => !!botCardHandle()?.focusChat());
    } else if (e.key === "Escape" && botIntent.current && !botHeldRef.current) {
      e.preventDefault();
      botIntent.dismiss();
    }
  };
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
  // …AND THE BOT CARD'S (below): one word to the page for either pinned card
  // (`anyPinned`), and the blur onto the frame closes whichever is pinned
  // and not held.
  // …AND THE + POPOVER'S AND THE TASKS CARD'S (owner, 2026-11-08: the gaps
  // between window A's cards closed — a press into the frame closes any
  // pinned card, and the page stops handing the frame the focus on a mere
  // pointer enter while any is up). The popover goes the chassis' way
  // (`shutPop`, declared with its close signal below; a card mid-Create
  // keeps its place).
  const tasksCard = useTasksCard();
  // …AND THE BOTS CARD'S (2026-11-09): the Bots door's own window A card
  // (BotsPeek, bots-peek-store), the Tasks card's twin in every rule below.
  const botsCard = useBotsCard();
  // …AND THE GIT CARD'S (2026-11-10): the Git door's window A card (GitPeek,
  // git-peek-store), the Bots card's twin in every rule below.
  const gitCard = useGitCard();
  const pinnedUp = pinned && peekKey !== null;
  const botPinnedUp = botPinned && botKey !== null;
  const popPinnedUp = !!card?.pop && !!card.pinned;
  const tasksHeldUp = navDoors && tasksCard.up && tasksCard.pinned;
  const botsHeldUp = navDoors && botsCard.up && botsCard.pinned;
  const gitHeldUp = navDoors && gitCard.up && gitCard.pinned;
  const anyPinned = pinnedUp || botPinnedUp || popPinnedUp || tasksHeldUp || botsHeldUp || gitHeldUp;
  const pinnedNow = useRef({ task: pinnedUp, bot: botPinnedUp, pop: popPinnedUp, tasks: tasksHeldUp, bots: botsHeldUp, git: gitHeldUp });
  pinnedNow.current = { task: pinnedUp, bot: botPinnedUp, pop: popPinnedUp, tasks: tasksHeldUp, bots: botsHeldUp, git: gitHeldUp };
  const shutPopRef = useRef<() => boolean>(() => false);
  const pinnedRef = useRef(onCardPinned);
  pinnedRef.current = onCardPinned;
  useEffect(() => {
    if (!anyPinned) return;
    pinnedRef.current?.(true);
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const a = document.activeElement;
        if (!(a instanceof HTMLIFrameElement)) return;
        if (pinnedNow.current.task && !heldRef.current && !document.getElementById(PEEK_ID)?.contains(a)) intent.dismiss();
        if (pinnedNow.current.bot && !botHeldRef.current && !document.getElementById(BOT_PEEK_ID)?.contains(a)) botIntent.dismiss();
        if (pinnedNow.current.tasks && !document.getElementById(TASKS_PEEK_ID)?.contains(a)) closeTasksCard();
        if (pinnedNow.current.bots && !document.getElementById(BOTS_PEEK_ID)?.contains(a)) closeBotsCard();
        if (pinnedNow.current.git && !document.getElementById(GIT_PEEK_ID)?.contains(a)) closeGitCard();
        if (pinnedNow.current.pop && !document.querySelector(".modal-popover")?.contains(a)) shutPopRef.current();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
      pinnedRef.current?.(false);
    };
  }, [anyPinned, intent, botIntent]);
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

  // ArrowLeft on a favourite's ring: the keyboard's way into ITS card (window
  // B), the same open the press makes; Escape lets the hover go.
  const onFavRingKey = (t: TaskPulseTask, href: string) => (e: ReactKeyboardEvent<HTMLAnchorElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      dismissFavHover();
      requestFavFocus(t.key);
      openFavourite(t, href);
    } else if (e.key === "Escape") {
      dismissFavHover();
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
  const [opening, setOpening] = useState(false);
  const plusRef = useRef<HTMLButtonElement | null>(null);
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
  const ringOrder = JSON.stringify([seats.map((x) => x.pulse.key), favs.map((x) => x.pulse.key), bots.map((b) => b.id)]);
  const ringsWere = useRef(ringOrder);
  useLayoutEffect(() => {
    if (ringsWere.current === ringOrder) return;
    ringsWere.current = ringOrder;
    if (lastPointer.current) stillAt.current = lastPointer.current;
  }, [ringOrder]);
  // `to`: the section's window — the list's card (A) or the favourites' (B).
  const enterRing = (key: string, e: ReactPointerEvent<HTMLAnchorElement>, to: PeekIntent) => {
    if (dragging(e)) return;
    const at = stillAt.current;
    if (at && Math.abs(e.clientX - at.x) < 2 && Math.abs(e.clientY - at.y) < 2) return;
    stillAt.current = null;
    // Window A's ring, with another of window A's cards pinned (the +
    // popover, the Tasks card): no hover (`aHovers` below).
    if (to === intent && !aHovers()) return;
    to.enterRing(key, "pointer");
  };
  const waiting = newTask.waiting;
  // THE POPOVER'S HOVER PREVIEW AND PIN (owner, 2026-11-07; plus-pop-lib.ts),
  // with the nav doors: the pointer resting on the + — or on a DRAFT's ring,
  // "exactly like the +", on that draft's record — shows the card unpinned,
  // a press on the door (or inside the card) pins it, and a pinned card
  // closes by its × only. The preview's own close is the chassis' way (Modal
  // `popover.closeSignal`), so it fades like any other.
  const [cardCloseSignal, setCardCloseSignal] = useState(0);
  const newTaskRef = useRef(newTask);
  newTaskRef.current = newTask;
  const plusSeedSeq = useRef(0);
  const plusSeed = useRef<{ door: string; card: Omit<Card, "seq"> } | null>(null);
  // The draft rings' doors, by key: the row and its href as the ring last
  // handed them over (hover or press), for the read and its fallbacks.
  const draftDoors = useRef(new Map<string, { row: Task; href: string }>());
  // What the popover stands beside: the + or the draft's ring (Modal
  // `popover.anchor`), set as its card opens.
  const popAnchor = useRef<HTMLElement | null>(null);
  const readDraftRef = useRef<(row: Task) => Promise<DraftRead>>(null!);
  const plusIntentRef = useRef<PlusPopIntent | null>(null);
  if (!plusIntentRef.current) {
    plusIntentRef.current = new PlusPopIntent({
      seed: (door) => {
        const seq = ++plusSeedSeq.current;
        plusSeed.current = null;
        const live = () => alive.current && seq === plusSeedSeq.current;
        if (door === PLUS_DOOR) {
          // As they were at the hover's start (header of `Card`).
          const target = newTaskRef.current.target;
          const where = newTaskRef.current.where;
          void cardSeed().then((got) => {
            if (!live()) return;
            plusSeed.current = { door, card: { target, where, ...got } };
            plusIntentRef.current?.seeded(door);
          });
          return;
        }
        const at = draftDoors.current.get(door);
        void (at ? readDraftRef.current(at.row) : Promise.resolve<DraftRead>({ kind: "gone" })).then((read) => {
          if (!live()) return;
          if (read.kind === "card") {
            plusSeed.current = { door, card: read.card };
            plusIntentRef.current?.seeded(door);
            return;
          }
          // Nothing to show: a hover shows nothing; a press takes the
          // draft door's own fallbacks (`openDraftCard`'s header).
          const pressed = plusIntentRef.current?.failed(door) ?? false;
          if (read.kind === "gone") refreshListing();
          else if (pressed && at) onOpenChatRef.current(at.href);
        });
      },
      cancelSeed: () => {
        plusSeedSeq.current++;
        plusSeed.current = null;
      },
      open: (door, pinned) => {
        const seed = plusSeed.current;
        if (!seed || seed.door !== door) return;
        // A draft's card stands where window A does — level with the list's
        // first ring (`draftCardBox`) — whichever draft's ring opened it; its
        // anchor is the whole list, so a press on another draft's ring is
        // that door's press, not a press outside the card.
        popAnchor.current =
          door === PLUS_DOOR ? plusRef.current : (listRef.current ?? ringEls.current.get(door) ?? plusRef.current);
        // A popover it replaces flushes its own draft as it unmounts
        // (NewJobModal's autosave): re-read so that ring shows.
        if (cardRef.current?.pop) refreshListing();
        setCard({ seq: Date.now(), ...seed.card, pop: true, pinned, door });
      },
      pin: () => setCard((c) => (c?.pop ? { ...c, pinned: true } : c)),
      close: () => setCardCloseSignal((n) => n + 1),
    });
  }
  const plusIntent = plusIntentRef.current;
  useEffect(() => () => plusIntent.dispose(), [plusIntent]);
  // A CARD THAT CAME ACROSS A PAGE HOP (plus-card-store): a pinned one is
  // this rail's now — beside this rail's + (or list), and this rail's intent
  // knows it is up, so a press on the + closes it the way it would have on
  // the page it was opened on. A hover's preview does not travel. A LAYOUT
  // effect, declared before the publish below: the anchor must be set before
  // the host re-renders the card with this rail's `placeKey`, or Modal
  // measures off a null anchor and keeps the last page's box (Bugbot #2220).
  useLayoutEffect(() => {
    const c = cardRef.current;
    if (!c?.pop) return;
    if (!c.pinned) {
      setCard(null);
      return;
    }
    popAnchor.current = !c.door || c.door === PLUS_DOOR ? plusRef.current : (listRef.current ?? plusRef.current);
    plusIntent.adopt(c.door || PLUS_DOOR, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // ONE CARD BESIDE THE RAIL (owner, 2026-11-07: "either new task thing
  // should be open or window A — new task thing is like window A but diff
  // UI"): the New task popover and the rail's other cards — window A (the
  // list's chat card), window B (a favourite's hover or open card, a bot's
  // card) — never stand together, hover or pinned. Whichever opened last
  // wins: the popover opening (a + hover, a + press, a draft's ring) puts
  // the others down; one of them opening closes the popover the chassis'
  // way (its draft autosave keeps the words) — unless the card is mid-Create
  // (its × held, `busy`), when the other one goes instead.
  // …WINDOW A ONLY (owner, 2026-11-08: "why is my hovering in window A's
  // section affecting something in window B, and why does hovering close
  // my clicked window"): the popover IS window A, so it trades places with
  // the list's card and the Tasks card alone. Window B — a favourite's hover
  // or open card, a bot's card — is the other section's: the popover neither
  // closes it nor is closed by it (the Tasks card's rule, below); it keeps
  // CLEAR of it instead (`popPlace`, rail-peek-lib `placeClear`). Until
  // 2026-11-08 a + hover put window B's clicked card down, and a favourite's
  // hover put the pinned popover down.
  const popSeq = card?.pop ? card.seq : null;
  useEffect(() => {
    if (popSeq === null) return;
    setHeld(false);
    intent.dismiss();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popSeq]);
  useEffect(() => {
    if (peekKey === null || popSeq === null) return;
    // No × at all while Create is pending (Modal draws none when busy): as
    // blocked as a disabled one (CodeRabbit on #2189).
    const busyPop = popBusy(railRef.current);
    if (busyPop) {
      setHeld(false);
      intent.dismiss();
      return;
    }
    setCardCloseSignal((n) => n + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [peekKey]);
  // CLOSE THE POPOVER THE CHASSIS' WAY (its fade, its draft autosave) — unless
  // it is mid-Create (its × held). True when it went.
  const shutPop = useCallback((): boolean => {
    if (!cardRef.current?.pop) return false;
    const busyPop = popBusy(railRef.current);
    if (busyPop) return false;
    setCardCloseSignal((n) => n + 1);
    return true;
  }, []);
  shutPopRef.current = shutPop;
  // …AND THE TASKS CARD (TasksPeek; owner, 2026-11-07) IS WINDOW A'S: it
  // stands where the list's card and the + popover stand, and takes their
  // turns with them — its opening (a hover's preview or a press) puts down
  // the list's card and the + popover (unless that one is mid-Create, when
  // the Tasks card goes instead), and either of those opening closes it.
  // NEVER WINDOW B (owner, 2026-11-08: "why does window B close when the
  // Tasks window is open — that is a regression; the Tasks window is like
  // window A, it shouldn't affect anything else"): the favourite's card, its
  // hover and the bot's card are the other section's, untouched both ways;
  // the Tasks card keeps clear of them instead (TasksPeek `avoid`).
  // (`tasksCard` is read above, with the pinned cards' frame rule.)
  const tasksSeq = navDoors && tasksCard.up ? tasksCard.seq : null;
  const tasksUpRef = useRef(false);
  tasksUpRef.current = tasksSeq !== null;
  // …AND THE BOTS CARD (2026-11-09), window A's too: the two door cards put
  // each other down, and the list's card and the + popover put both down.
  const botsSeq = navDoors && botsCard.up ? botsCard.seq : null;
  const botsUpRef = useRef(false);
  botsUpRef.current = botsSeq !== null;
  // …AND THE GIT CARD (2026-11-10), the third door card: the three put each
  // other down, and the list's card and the + popover put all three down.
  const gitSeq = navDoors && gitCard.up ? gitCard.seq : null;
  const gitUpRef = useRef(false);
  gitUpRef.current = gitSeq !== null;
  useEffect(() => {
    if (tasksSeq === null) return;
    closeBotsCard();
    closeGitCard();
    if (cardRef.current?.pop) {
      const busyPop = popBusy(railRef.current);
      if (busyPop) {
        closeTasksCard();
        return;
      }
      setCardCloseSignal((n) => n + 1);
    }
    setHeld(false);
    intent.dismiss();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasksSeq]);
  useEffect(() => {
    if (botsSeq === null) return;
    closeTasksCard();
    closeGitCard();
    if (cardRef.current?.pop) {
      const busyPop = popBusy(railRef.current);
      if (busyPop) {
        closeBotsCard();
        return;
      }
      setCardCloseSignal((n) => n + 1);
    }
    setHeld(false);
    intent.dismiss();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botsSeq]);
  useEffect(() => {
    if (gitSeq === null) return;
    closeTasksCard();
    closeBotsCard();
    if (cardRef.current?.pop) {
      const busyPop = popBusy(railRef.current);
      if (busyPop) {
        closeGitCard();
        return;
      }
      setCardCloseSignal((n) => n + 1);
    }
    setHeld(false);
    intent.dismiss();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gitSeq]);
  // …up OR still a hover's pending read (CodeRabbit on #2189: the pointer
  // resting on the Tasks door while the + pins by keyboard): `dismiss` is a
  // no-op on a card that is not up, and cancels the open that was coming.
  useEffect(() => {
    if ((peekKey ?? popSeq) !== null) {
      closeTasksCard();
      closeBotsCard();
      closeGitCard();
    }
  }, [peekKey, popSeq]);
  // ANOTHER WINDOW A CARD PINNED (the Tasks card by its door, the list card
  // by a ring) while a + or draft hover's read is still out: that preview
  // must not open over it when its delay runs out (plus-pop-lib
  // `cancelPreview`; CodeRabbit on #2189). `plusIntent` is declared below;
  // the effect runs after the render that declared it.
  const tasksPinnedUp = navDoors && tasksCard.up && tasksCard.pinned;
  const botsPinnedUp = navDoors && botsCard.up && botsCard.pinned;
  const gitPinnedUp = navDoors && gitCard.up && gitCard.pinned;
  const listPinnedUp = pinned && peekKey !== null;
  useEffect(() => {
    if (tasksPinnedUp || botsPinnedUp || gitPinnedUp || listPinnedUp) plusIntentRef.current?.cancelPreview();
  }, [tasksPinnedUp, botsPinnedUp, gitPinnedUp, listPinnedUp]);
  // ESCAPE WITH THE FOCUS NOWHERE closes a pinned + popover or Tasks card, as
  // it closes the pinned list card (its rule above; owner, 2026-11-08: the
  // gaps between window A's cards closed). Inside either, Escape is its own.
  useEffect(() => {
    if (!popPinnedUp && !tasksPinnedUp && !botsPinnedUp && !gitPinnedUp) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target !== document.body && e.target !== document.documentElement) return;
      e.preventDefault();
      if (tasksPinnedUp) closeTasksCard();
      if (botsPinnedUp) closeBotsCard();
      if (gitPinnedUp) closeGitCard();
      if (popPinnedUp) shutPop();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [popPinnedUp, tasksPinnedUp, botsPinnedUp, gitPinnedUp, shutPop]);
  // BESIDE THE OPEN PANE IT GOES THE WAY WINDOW A GOES (owner, 2026-11-08:
  // "if the sidebar is open, a press opens in the sidebar and closes window
  // A; in the expanded sidebar, when the sidebar's chat changes, close window
  // A"): the pane opening under it, and the pane's chat changing while the
  // pane is open, both put the Tasks card down — the list card's own two
  // rules (`paneWasOpen` below, and a ring's pane press). On the turns only,
  // read off refs.
  const tasksPaneWasOpen = useRef(paneOpen);
  const paneChatWas = useRef(activeSessionId);
  useLayoutEffect(() => {
    const wasOpen = tasksPaneWasOpen.current;
    tasksPaneWasOpen.current = paneOpen;
    const chatWas = paneChatWas.current;
    paneChatWas.current = activeSessionId;
    if (!paneOpen || (wasOpen && chatWas === activeSessionId)) return;
    if (tasksUpRef.current) closeTasksCard();
    if (botsUpRef.current) closeBotsCard();
    if (gitUpRef.current) closeGitCard();
    // …and the + popover, the same way (owner, 2026-11-08: the gaps between
    // window A's cards closed).
    shutPop();
  }, [paneOpen, activeSessionId, shutPop]);
  // AFTER A CLICK, ONLY A CLICK CHANGES WINDOW A (owner, 2026-11-08;
  // claude-rail-lib `windowAHovers`): while any window A card is pinned —
  // the list's chat card, the + popover, the Tasks card — or a draft's modal
  // card is up, none of window A's doors previews on a hover: not the list's
  // rings, not the +, not a draft's ring, not the Tasks door. Each door's own
  // machine already sits still for its own pinned card; this is the rule
  // across them. Read at the hover, off this render's state.
  const aHovers = () =>
    windowAHovers({
      listPinned: pinned && peekKey !== null,
      popPinned: !!card?.pop && !!card.pinned,
      tasksPinned: tasksCard.up && tasksCard.pinned,
      botsPinned: botsCard.up && botsCard.pinned,
      gitPinned: gitCard.up && gitCard.pinned,
      modalCard: !!card && !card.pop,
    });
  // A hover on the + previews only where its press opens the popover card,
  // and never over a draft's (modal) card or another pinned window A card.
  const plusHovers = () => navDoors && !waiting && aHovers();
  // WINDOW A'S SIZE, DRAGGED ON THE + POPOVER (owner, 2026-11-08: "that modal
  // is supposed to be window A, but resize is not working for that modal"):
  // the chassis' grips (Modal `popover.resize`) report the size let go at,
  // remembered as the list card's own (`canvasRailPeek`, RailPeek and
  // TasksPeek's key) — and read back on the next render (the layout tick),
  // which re-places the card level with its door.
  const [, setLayoutTick] = useState(0);
  const savePopSize = useCallback((size: { width: number; height: number }, changed: { width: boolean; height: boolean }) => {
    // Only a dimension the drag moved, past the grips' minimums (TasksPeek
    // `endDrag`): the other keeps what was remembered — a width the window
    // clamped is not saved over it by a height-only drag, nor a height a
    // short window pressed under the floor. Read off the updater's own doc,
    // so a replay onto the server's copy (ui-layout, before hydrate) keeps
    // that copy's value, not this document's default (CodeRabbit on #2189).
    patchUiLayout((d) => {
      const prev = railCardSize(d.canvasRailPeek ?? null);
      const width = changed.width && size.width >= PEEK_MIN_WIDTH ? size.width : prev.width;
      const height = changed.height && size.height >= PEEK_MIN_HEIGHT ? size.height : prev.height;
      return d.canvasRailPeek?.width === width && d.canvasRailPeek?.height === height ? d : { ...d, canvasRailPeek: { width, height } };
    });
    // A patch tells no subscriber (ui-layout `subscribeUiLayout`: hydrates
    // only) — this render re-reads it itself (Bugbot on #2189).
    setLayoutTick((n) => n + 1);
  }, []);
  // …and a hydrate landing after the first render (another window's drag).
  useEffect(() => subscribeUiLayout(() => setLayoutTick((n) => n + 1)), []);
  // WHERE A DRAFT'S POPOVER STANDS (owner, 2026-11-07: it "jumps" from ring
  // to ring; "it must stand like window A does"): its top level with the
  // list's FIRST ring, clamped to the list's scrolled box (rail-peek-lib
  // `clampAnchor`, RailPeek's own) — so moving between draft rings changes
  // the card's words, never its place. Null off the list (the + then).
  const draftCardBox = useCallback(() => {
    const first = listRef.current?.querySelector<HTMLElement>("a[data-task-key]");
    if (!first) return null;
    const scroller = first.closest(".claude-rail-chats");
    return clampAnchor(first.getBoundingClientRect(), scroller ? scroller.getBoundingClientRect() : null);
  }, []);
  // WHERE THE POPOVER STANDS (Modal `popover.place`): as the Tasks card does
  // — level with its door (the +, or the list's first ring for a draft's),
  // beside the rail, inside the workspace row, and clear of window B's card
  // (`favBox`): above it when there is room, beside it otherwise.
  const popPlace = useCallback(
    (want: { width: number; height: number }) => {
      const rail = railRef.current;
      const row = rail && rowOf(rail);
      const door = cardRef.current?.door;
      const anchor = door && door !== PLUS_DOOR ? draftCardBox() : plusRef.current?.getBoundingClientRect();
      if (!rail || !row || !anchor) return null;
      return placeClear(anchor, rail.getBoundingClientRect(), row.getBoundingClientRect(), favBox, {
        width: want.width,
        maxHeight: want.height,
      });
    },
    [favBox, draftCardBox],
  );
  // A DRAFT'S RING IS A POPOVER DOOR (owner, 2026-11-07: a draft "behaves
  // exactly like the +"): its hover previews the New task popover on its
  // record — window A's draft summary is gone — and its press pins it
  // (`pressRing` → `openDraftCard`). The ring's own reshape guard
  // (`stillAt`, `enterRing`) applies.
  const enterDraftDoor = (row: Task, href: string, e: ReactPointerEvent<HTMLAnchorElement>) => {
    if (dragging(e)) return;
    const at = stillAt.current;
    if (at && Math.abs(e.clientX - at.x) < 2 && Math.abs(e.clientY - at.y) < 2) return;
    stillAt.current = null;
    if (e.pointerType === "touch" || !aHovers()) return;
    if (!isChatDraftTask(row) && !row.draft_id) return;
    draftDoors.current.set(row.key, { row, href });
    plusIntent.enterPlus(row.key);
  };
  const openNewTask = () => {
    if (navDoors) {
      if (waiting || (card && !card.pop)) return;
      // A TOGGLE (owner, 2026-11-10): a press on the + while ITS card stands
      // pinned closes it, the chassis' way (busy / dirty guards, fade) — the
      // first press pinned the preview, the next one puts it down.
      if (card?.pop && card.pinned && (card.door || PLUS_DOOR) === PLUS_DOOR) {
        setCardCloseSignal((n) => n + 1);
        return;
      }
      plusIntent.press(PLUS_DOOR);
      return;
    }
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
  const readDraftCard = (row: Task): Promise<DraftRead> => {
    const chat = isChatDraftTask(row);
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
      if (!chat && !row.draft_id) return resolve({ kind: "gone" });
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
    return Promise.all([cardSeed(), stored]).then(([got, read]): DraftRead =>
      read.kind !== "seed"
        ? read
        : {
            kind: "card",
            card: {
              ...base,
              ...got,
              draft: read.seed,
              chatKey: chat ? row.key : "",
              time: reopenTime(read.seed),
            },
          },
    );
  };
  readDraftRef.current = readDraftCard;
  const openDraftCard = (row: Task, href: string) => {
    if (!isChatDraftTask(row) && !row.draft_id) return;
    // With the nav doors (owner, 2026-11-07: "drafts open should open in the
    // new thing we added"): the popover's door for this draft — pinned, a
    // press — over whatever popover is up; a draft's modal card, or a
    // popover mid-Create, keeps its place.
    if (navDoors) {
      if (card && !card.pop) return;
      if (card && popBusy(railRef.current)) return;
      draftDoors.current.set(row.key, { row, href });
      plusIntent.press(row.key);
      return;
    }
    if (opening || card) return;
    setOpening(true);
    void readDraftCard(row).then((read) => {
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
      setCard({ seq: Date.now(), ...read.card });
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
  const openChat = (href: string): void | Promise<boolean> => {
    const seat = seatByHref.get(href);
    // A favourite's door (its hover card's Open) moves the favourites' mark.
    // Window A's doors are the LIST's (2026-11-05: a favourite's ring opens
    // window B, never this card): the list's mark, whatever the chat's star.
    if (seat) setLastChat(lastChatId(seat.pulse));
    if (seat?.row && isDraftTask(seat.row)) openDraftCard(seat.row, href);
    else return onOpenChat(href);
  };
  const openChatRef = useRef(openChat);
  openChatRef.current = openChat;
  const onOpenChatRef = useRef(onOpenChat);
  onOpenChatRef.current = onOpenChat;
  // A RING'S ORDINARY PRESS (header): the card, shown now and pinned, with
  // the keyboard on its way to the composer — or, with no room for a card,
  // the chat in the pane. Also a favourite's press that the server refused
  // (`openFavourite`), which lands a round trip later: hence the ref.
  const pressRing = (t: TaskPulseTask, href: string, keyboard: boolean) => {
    const how = ringPress({ paneOpen, favsOn, peekOn, noRoom });
    // BESIDE THE OPEN PANE the press is the same press (owner, 2026-11-08:
    // "if the sidebar is expanded we don't show window A — we will instead
    // show window A, and not change the expanded sidebar unless the user
    // clicks Open from window A"): the card pins; the pane keeps its chat —
    // the pane's own chat included, mounted again in the card.
    if (how === "card") {
      // A DRAFT'S RING, with the nav doors (owner, 2026-11-07: "drafts open
      // should open in the new thing we added"): the New task popover on
      // the draft, pinned — not window A. A hover's card coming on this ring
      // is called off first.
      const seat = seatByHref.get(href);
      if (navDoors && seat?.row && isDraftTask(seat.row)) {
        hideHint();
        setHeld(false);
        intent.dismiss();
        openChat(href);
        return;
      }
      // A TOGGLE (owner, 2026-11-06): the ring of the chat whose card a press
      // pinned closes it; a hover card on it is pinned by the press.
      if (pinNow.current.favsOn && pinNow.current.pinned && pinNow.current.peekKey === t.key) {
        hideHint();
        setHeld(false);
        intent.dismiss();
        return;
      }
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
  // THE PANE OPENING UNDER A PINNED LIST CARD (header): the card goes. On
  // the turn from shut to open only; read off refs, the turn is what decides.
  const pinNow = useRef({ pinned, peekKey, favsOn });
  pinNow.current = { pinned, peekKey, favsOn };
  const paneWasOpen = useRef(paneOpen);
  // THE PANE OPENING UNDER A PINNED LIST CARD (owner, 2026-11-05 evening:
  // "the expand button should only open the Recent chats sidebar, not an
  // item active — for that it has the Open button in the popover already"):
  // the card goes, and NOTHING is opened in the pane — the pane stands on
  // its home, whose Recent list marks the chat the card was on (the list's
  // last press, apps/claude Lists `selected`). The hand-over of that
  // morning (the pane taking the pinned chat) is retired.
  useLayoutEffect(() => {
    const wasOpen = paneWasOpen.current;
    paneWasOpen.current = paneOpen;
    if (wasOpen || !paneOpen || !pinNow.current.favsOn || !pinNow.current.pinned) return;
    setHeld(false);
    intent.dismiss();
  }, [paneOpen, intent]);
  // THE PANE COLLAPSING GIVES WINDOW B BACK — AND NOTHING OF WINDOW A
  // (owner, 2026-11-06: "if I click Open from a normal task and close the
  // sidebar it pops the window up again — we remove this; we already show
  // the highlight by last selected, that should be enough"; and "if I have
  // a fav window which expanded the sidebar and opened the chat, when I
  // close that sidebar it should open the fav popup window again"): on the
  // turn from open to shut, the favourite whose Open door expanded the pane
  // (FavouritePeek, fav-peek-store `takeFavRestore`) opens again in this
  // window; the list's card stays down, its `.is-last` ring the mark. The
  // restore of 2026-11-05 (`collapseRestore`) is gone.
  const restoreWasOpen = useRef(paneOpen);
  useLayoutEffect(() => {
    const wasOpen = restoreWasOpen.current;
    restoreWasOpen.current = paneOpen;
    if (!wasOpen || paneOpen || !pinNow.current.favsOn) return;
    const key = takeFavRestore();
    if (key) setFavOpen(key);
  }, [paneOpen]);
  // …and a rail beside an OPEN pane confirms an armed restore (fav-peek-
  // store `confirmFavRestore`): the door did open the pane. Every render:
  // the door arms it between renders, pane open or not yet.
  useEffect(() => {
    if (paneOpen) confirmFavRestore();
  });
  // THE + BESIDE THE OPEN PANE (header; claude-rail-lib `plusDoor`): the
  // pane's home, and the keyboard into its composer — retried until it
  // stands (the page may remount its chat, the leave guard may ask), and
  // given up the moment the reader presses or types anything themselves.
  // THE + IS THE NEW TASK CARD, EVERYWHERE (claude-rail-lib `plusDoor`;
  // owner, 2026-11-08: "any time you click the + we show the task modal only
  // and not focus the expanded sidebar"): beside the open pane as shut. The
  // pane-home door and its composer focus (2026-11-05) are gone.
  const plusLabel = waiting ?? (newTask.on ? `New task on ${newTask.on}` : "New task");
  // THE NAV DOORS' DOTS (`navDoors`): the left sidebar's Tasks and Bots row
  // dots (GlobalSidebar `tasksDot` / `botsDot`), read from the same pulses —
  // red waits on the reader, yellow runs, green is news not yet seen (the
  // Tasks one suppressed while the Tasks page is the one on screen).
  const tasksPulse = useTasksPulse();
  // The Bots door only where the server offers Bots (`botsOffered`, above).
  const botsDoor = botsOffered && navDoors;
  const botsPulse = useBotsPulse(botsDoor);
  // The Search door only where the app has a launcher panel to show (the
  // Mac desktop app; `useLauncherAvailable`).
  const launcherOffered = useLauncherAvailable(navDoors);
  const searchDoor = navDoors && launcherOffered;
  // THE DOORS WEAR THEIR WINDOW'S OPEN STATE (owner, 2026-11-09: "show
  // highlight only when the window is open, so we know this one made it
  // open — for +, Tasks, Bots and Search as well"): `.is-open` while the
  // door's window stands (a hover's preview), `.is-pinned` beside it once a
  // press holds it — the list rings' own two steps (claude-rail.css). The
  // Search door's window is the launcher panel (`useLauncherOpen`); the Bots
  // door's is its card (BotsPeek), as the Tasks door's.
  const launcher = useLauncherOpen();
  const doorOpen = (up: boolean, pinnedUp: boolean) => (up ? " is-open" + (pinnedUp ? " is-pinned" : "") : "");
  const here = typeof location === "undefined" ? "" : location.pathname;
  const tasksHere = here === "/tasks";
  const botsHere = isBotsPath(here);
  // THE BOT CARD COMES BACK WITH THE RAIL (owner, 2026-11-08: "bots open in
  // window B close when I switch view, which is wrong — tasks open in window
  // B don't; a bot's card should only close on a switch to a task or another
  // bot, or its ×"): the favourite's card is the shell's and stands through
  // a page's view switch; this rail is the page's and remounts with it. The
  // window's mark (last-chat `botOpen`) names the pinned bot; once the pulse
  // lists it, the card is pinned again — unless a favourite's card holds
  // window B, or the reader landed on the Bots page (the ring is the door
  // there): then the mark goes. Once per mount, after the pulse's first
  // answer.
  const botRestored = useRef(false);
  useEffect(() => {
    if (botRestored.current || !peekOn || botNoRoom) return;
    // The shell's card is up already (a view switch, not a reload): nothing
    // to restore.
    if (botIntent.current !== null) {
      botRestored.current = true;
      return;
    }
    const id = botOpen();
    if (!id) {
      botRestored.current = true;
      return;
    }
    if (!bots.some((b) => b.id === id)) return;
    botRestored.current = true;
    // A favourite's card holds window B: the mark goes. (The Bots page is
    // no reason — its rings open the card too; CodeRabbit on #2201.)
    if (openFavKey) {
      setBotOpen(null);
      return;
    }
    botIntent.press(id);
  }, [peekOn, botNoRoom, bots, openFavKey, botIntent]);
  const tasksDot =
    tasksPulse.attention + tasksPulse.blocked > 0
      ? "is-attention"
      : tasksPulse.running > 0
        ? "is-running"
        : !tasksHere && tasksPulse.unseen > 0
          ? "is-unread"
          : null;
  const botsDot =
    botsPulse.waiting > 0 ? "is-attention" : botsPulse.running > 0 ? "is-running" : botsPulse.unread > 0 ? "is-unread" : null;
  const tasksTip = pulseTitle(tasksPulse);
  const botsTip = botsPulseTitle(botsPulse);
  // THE TASKS DOOR OPENS THE TASKS CARD (TasksPeek; owner, 2026-11-07):
  // resting on it previews the card (window A's timings, tasks-peek-store's
  // intent), a press pins it, a press while pinned closes it; a modified press
  // is still the link's, and with no card to show (none mounted, no room) the
  // door is the plain link to /tasks it was (tasks-peek-store
  // `tasksDoorAction`). The rail registers the door so the card stands level
  // with it.
  const tasksDoorRef = useRef<HTMLAnchorElement | null>(null);
  useLayoutEffect(() => {
    const el = railRef.current;
    const door = tasksDoorRef.current;
    return navDoors && shown && el && door ? registerTasksDoor(el, door) : undefined;
  }, [navDoors, shown]);
  const tasksUp = navDoors && tasksCard.up;
  const pressTasksDoor = (e: ReactMouseEvent<HTMLAnchorElement>) => {
    const act = tasksDoorAction({
      modified: e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0,
      ready: tasksCard.ready,
      pinned: tasksCard.up && tasksCard.pinned,
    });
    if (act === "native") return;
    e.preventDefault();
    hideHint();
    if (act === "navigate") void navigateUrl("/tasks");
    else if (act === "close") closeTasksCard();
    else {
      tasksCardIntent().press(TASKS_CARD_KEY);
      // An Enter on the door (`detail` 0: no pointer) takes the keyboard into
      // the card, as a ring's does into the list card (CodeRabbit,
      // 2026-11-08: left on the door, the next Tab reached the + instead).
      if (e.detail === 0) cardFocusSoon(TASKS_PEEK_ID, focusTasksCard);
    }
  };
  // THE BOTS DOOR OPENS THE BOTS CARD (BotsPeek; owner, 2026-11-09), the
  // Tasks door's rules to the letter: a hover previews, a press pins, a
  // press while pinned closes; a modified press is the link's, and with no
  // card to show the door is the plain link to /bots (bots-peek-store
  // `botsDoorAction`). Registered so the card stands level with it.
  const botsDoorRef = useRef<HTMLAnchorElement | null>(null);
  useLayoutEffect(() => {
    const el = railRef.current;
    const door = botsDoorRef.current;
    return navDoors && shown && el && door ? registerBotsDoor(el, door) : undefined;
  }, [navDoors, shown, botsDoor]);
  const botsUp = navDoors && botsCard.up;
  const pressBotsDoor = (e: ReactMouseEvent<HTMLAnchorElement>) => {
    const act = botsDoorAction({
      modified: e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0,
      ready: botsCard.ready,
      pinned: botsCard.up && botsCard.pinned,
    });
    if (act === "native") return;
    e.preventDefault();
    hideHint();
    if (act === "navigate") void navigateUrl("/bots");
    else if (act === "close") closeBotsCard();
    else {
      botsCardIntent().press(BOTS_CARD_KEY);
      if (e.detail === 0) cardFocusSoon(BOTS_PEEK_ID, focusBotsCard);
    }
  };
  // THE GIT DOOR OPENS THE GIT CARD (GitPeek; owner, 2026-11-10), the Bots
  // door's rules to the letter: a hover previews, a press pins, a press
  // while pinned closes. A button, not a link — no page of its own: with no
  // card to show (none mounted, no room) the press opens the page's Git
  // panel (git-peek-store `gitDoorAction`). Under the rings, past a hairline,
  // only while the page's path sits in a repository (`gitRepo`).
  const gitDoorRef = useRef<HTMLButtonElement | null>(null);
  const gitDoorShown = gitRepo !== null;
  useLayoutEffect(() => {
    const el = railRef.current;
    const door = gitDoorRef.current;
    return navDoors && shown && el && door ? registerGitDoor(el, door) : undefined;
  }, [navDoors, shown, gitDoorShown]);
  const gitUp = navDoors && gitCard.up;
  const pressGitDoor = (e: ReactMouseEvent<HTMLButtonElement>) => {
    const act = gitDoorAction({ ready: gitCard.ready, pinned: gitCard.up && gitCard.pinned });
    hideHint();
    if (act === "open") openGitRef.current?.();
    else if (act === "close") closeGitCard();
    else {
      // This rail opens it (an explorer split's two rails, two repositories).
      setGitCardRail(railRef.current);
      gitCardIntent().press(GIT_CARD_KEY);
      if (e.detail === 0) cardFocusSoon(GIT_PEEK_ID, focusGitCard);
    }
  };
  const gitDots = gitRepo ? gitDoorDots({ checking: railGit.checking, checked: railGit.checked, repo: gitRepo }) : null;
  const gitLabel = gitRepo
    ? [
        `Git, on ${gitBranch(gitRepo)}`,
        ...gitSyncWords(gitRepo),
        ...(gitDots?.top && gitDots.top !== "push" ? [GIT_DOT_WORDS[gitDots.top]] : []),
      ].join(", ")
    : "Git";

  // ONE ACTIVE RING PER SECTION (header; rail-peek-lib `activeRing`). The
  // list's section is its rings (no outsider in the foot since 2026-11-06). With the
  // favourites off the list keeps every mark it had (flag-off parity).
  const listSection = baseSeats;
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
  // SCROLL, NEVER POSITIONS (owner, 2026-11-05: "it is just moving scroll,
  // not moving positions"): a new place starts the list at its top; the
  // list's active ring — the last selected, the chat the pane gave back on
  // collapsing — is scrolled into the box when it stands outside it, to the
  // bottom edge when it is below (the 14th ring as the 10th row), the top
  // when above. Rings in view are left where they are.
  const placeWas = useRef(place);
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    if (placeWas.current !== place) {
      placeWas.current = place;
      el.scrollTop = 0;
    }
    const ring = activeList ? ringEls.current.get(activeList) : null;
    if (ring && el.contains(ring)) {
      const r = ring.getBoundingClientRect();
      const b = el.getBoundingClientRect();
      if (r.bottom > b.bottom) el.scrollTop += r.bottom - b.bottom + 2;
      else if (r.top < b.top) el.scrollTop -= b.top - r.top + 2;
    }
    measureEdges();
  }, [place, activeList, measureEdges, hideList]);

  // ONE RING, as the list draws it (and as the favourites' section does).
  const ringOf = ({ pulse: t, row, off }: RailRow, foot = false) => {
              // The live status is the pulse's (fresher than the listing).
              const merged = row ? { ...row, status: t.status } : t;
              // Hollow since the press, until the pulse agrees (header).
              const unread = t.unread > 0 && !dotCleared(marks.get(t.key), t);
              const line = cardTitleLine(merged);
              const reply = row ? row.last_reply || "" : "";
              const label = line.text || t.title || "Chat";
              // A DRAFT'S DOOR is the New task card (`draftHref`, as the List's
              // row press); every other row's is the chat's (`taskHref`). A
              // pulse-only draft (the listing not held yet) has neither for a
              // moment: it still gets its ring, the press waits for the
              // listing.
              const draft = !!row && isDraftTask(row);
              // A list draft's ring, with the nav doors: the popover's door
              // (`enterDraftDoor`), not window A's.
              const popDoor = navDoors && peekOn && !foot && draft && !!row;
              const href = seatHref({ pulse: t, row });
              // No door, no ring (as before): a form-scheduled row with no
              // session, or a pulse-only draft the listing has not named yet.
              if (!href) return null;
              const words = draft ? "draft" : statusWords(unread ? t : { ...t, unread: 0 }, !!row?.failed);
              const peeked = peekOn && (foot ? favHoverKey === t.key : peekKey === t.key);
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
                favHoverKey,
                botHeld: botUp !== null,
                // Window A held by the + popover or the Tasks card: the list's
                // rings say their title (`aHovers` shows them no card).
                windowAHeld:
                  (!!card?.pop && !!card.pinned) ||
                  (tasksCard.up && tasksCard.pinned) ||
                  (botsCard.up && botsCard.pinned) ||
                  (gitCard.up && gitCard.pinned),
                sectionSize: foot ? favs.length : baseSeats.length,
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
              // caption; so is every other favourite's ring while any favourite card is open.
              const favRing = wears.favRing;
              const titleOnly = wears.titleOnly;
              const captionless = peekOn && !titleOnly;
              // THE RING'S WINDOW (owner, 2026-11-05: two sections, two
              // windows): a favourite's ring talks to window B's hover
              // (fav-peek-store `favHoverIntent`), a list ring to this
              // rail's card — neither ever moves the other.
              const win = foot && favsOn ? favHoverIntent() : intent;
              // The list's item is a wrapper, the link is a link: `aria-haspopup`
              // and `aria-expanded` are not allowed on `listitem`, and a link
              // that opens a dialog is the shape they describe.
              return (
                <div key={t.key} role="listitem" className={`claude-rail-seat${off ? " is-off" : ""}`}>
                <a
                  ref={(el) => {
                    // THE LIST'S RINGS ANCHOR WINDOW A (header): with the
                    // favourites on, a favourite's ring is window B's
                    // (FavouritePeek, on the rail's bottom edge) and must
                    // not claim the chat's anchor — the same chat stands
                    // in both sections (2026-11-05), and a star pressed in
                    // window A moved the card onto the new foot ring.
                    // Favourites off, the foot is empty and every ring is
                    // the list's.
                    if (foot && favsOn) return;
                    if (el) ringEls.current.set(t.key, el);
                    else ringEls.current.delete(t.key);
                  }}
                  href={href}
                  // Found again by key when a card shuts under the keyboard
                  // (fav-peek-store `focusRail`).
                  data-task-key={t.key}
                  // A blocked, unstarred favourite wears `.is-blocked`
                  // (rail-peek-lib `favLike`): the section's ring, no star.
                  className={[
                    "sidebar-rail-btn",
                    "claude-rail-chat",
                    ...wears.classes,
                    // A draft's ring whose New task popover stands: its window
                    // is open (2026-11-09), the list rings' own marks.
                    ...(popDoor && card?.pop && card.door === t.key
                      ? ["peeked", ...(card.pinned ? ["is-pinned"] : [])].filter((c) => !wears.classes.includes(c))
                      : []),
                    ...(foot && !t.starred ? ["is-blocked"] : []),
                  ].join(" ")}
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
                  {...(popDoor
                    ? {
                        "aria-haspopup": "dialog" as const,
                        "aria-expanded": !!card?.pop && card.door === t.key,
                      }
                    : peekOn && wears.hovers
                    ? {
                        "aria-haspopup": "dialog" as const,
                        "aria-expanded": peeked,
                        "aria-keyshortcuts": "ArrowLeft",
                        ...(peeked ? { "aria-controls": foot ? FAV_PEEK_ID : PEEK_ID } : {}),
                        onKeyDown: foot ? onFavRingKey(t, href) : onRingKey(t.key),
                      }
                    : peekOn && favRing
                      ? {
                          // The open favourite's ring: its card is the
                          // shell's (FavouritePeek), a dialog of its own.
                          "aria-haspopup": "dialog" as const,
                          "aria-expanded": true,
                          "aria-controls": FAV_PEEK_ID,
                        }
                      : peekOn && foot && favsOn
                        ? {
                            // A SILENCED favourite's ring — another favourite's
                            // card, or a bot's, holds window B — hovers nothing,
                            // but the keyboard still takes the window
                            // (CodeRabbit on #2154): ArrowLeft opens this
                            // favourite there, as a press would.
                            "aria-keyshortcuts": "ArrowLeft",
                            onKeyDown: onFavRingKey(t, href),
                          }
                        : {})}
                  {...(popDoor && row && href
                    ? {
                        onPointerEnter: (e: ReactPointerEvent<HTMLAnchorElement>) => enterDraftDoor(row, href, e),
                        onPointerLeave: () => {
                          plusIntent.leavePlus();
                          win.leaveRing("pointer");
                        },
                        onBlur: () => win.leaveRing("focus"),
                      }
                    : peekOn
                    ? {
                        // The open favourite's ring opens no hover card, but
                        // a leave is always heard: the intent must not think
                        // the pointer still rests on a ring it left.
                        ...(!wears.hovers
                          ? {}
                          : {
                              onPointerEnter: (e: ReactPointerEvent<HTMLAnchorElement>) =>
                                enterRing(t.key, e, win),
                              // A focus HANDED here (a card shutting
                              // under the keyboard, fav-peek-store
                              // `focusRail`) opens no card.
                              onFocus: (e: ReactFocusEvent<HTMLAnchorElement>) => {
                                if (takeQuietFocus(e.currentTarget)) win.quietFocus(t.key);
                                // A Tab onto window A's ring is its hover
                                // (`aHovers`): nothing over another pinned
                                // window A card.
                                else if (win !== intent || aHovers()) win.enterRing(t.key, "focus");
                              },
                            }),
                        onPointerLeave: () => win.leaveRing("pointer"),
                        onBlur: () => win.leaveRing("focus"),
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
                    // favourite's press never pins this rail's card (claude-
                    // rail-lib `ringMarks`). The open favourite's ring — the
                    // foot's, or the list's a moment after its star — has its
                    // card up already: nothing more to open.
                    // WHATEVER THE PANE SHOWS (owner, 2026-11-05: "if the
                    // expanded sidebar is open with a fav task, we don't
                    // open and focus the expanded sidebar"): the press
                    // opens window B, the chat in both if need be.
                    if (foot && peekOn) {
                      hideHint();
                      // The hover (window B) becomes the open card, or goes.
                      dismissFavHover();
                      // ANY press — pointer or keyboard — lands in the
                      // favourite card's composer once it stands (fav-peek-
                      // store; Akshil, 2026-11-07: "when I click to open the
                      // window in normal tasks it focuses the input, but for
                      // fav tasks it doesn't"), the list card's own rule.
                      // A TOGGLE (owner, 2026-11-06): the open favourite's
                      // own ring closes its card; any other opens it (asking
                      // for the card's keyboard then — never for a close, or
                      // the wish would outlive it).
                      if (favRing) clearFavOpen(t.key);
                      else {
                        requestFavFocus(t.key);
                        openFavourite(t, href);
                      }
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

  // THE CARD, DRAWN BY THE SHELL'S HOST when one stands (plus-card-store):
  // this rail publishes how — its anchor, placement and doors — on every
  // render, and draws it itself only with no host (a rail rendered alone).
  const hosted = usePlusHosted();
  const popPlaceKey = useMemo(() => ({ favBox }), [favBox]);
  const renderCard = (card: Card) => (
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
        // it, the app folder, the explorer's folder or file — the app
        // page's Tasks tab's way (Scheduled.tsx `lockTarget`): a task
        // made from a page is a task about that page, so the path is
        // stated, not asked (the + is held until the page can say it).
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
          if (card.pop) plusIntent.closed();
          // A preview nobody touched left nothing behind: the reader's
          // first act inside it pins it (plus-pop-lib.ts).
          if (!card.pop || card.pinned) refreshListing();
        }}
        // "Something changed, re-read": the pulse will move the new row
        // onto the rail; the listing is re-read now so its caption and
        // rank are not 600ms behind its ring.
        onCreated={() => refreshListing()}
        // The landing card's chat lists (Recent chats, Pages, Snapshots):
        // a row goes through the rail's one door, as a ring's press does.
        onOpenChat={(href) => openChatRef.current(href)}
        // A draft row in its lists: the draft's own popover door, the
        // ring's press (`openDraftCard`) — the pressed draft fills this
        // window, as it fills the chat landing's box.
        onOpenDraft={(row) => {
          const href = draftHref(row) ?? taskHref(row);
          if (href) openDraftCard(row, href);
        }}
        // CREATE KEEPS THE CARD: a fresh mount (new `seq`, no draft) opening
        // on the new task's chat, so Back is an empty landing.
        initialChatEntry={card.chatEntry ?? null}
        initialChatSeed={card.chatSeed ?? null}
        onCreatedChat={(entryId, seed) =>
          setCard((c) => (c ? { ...c, seq: Date.now(), draft: null, chatKey: "", time: null, chatEntry: entryId, chatSeed: seed } : c))}
        // The +'s card with the rail flag on: the same card as a popover
        // beside the rail (`Card.pop`), top-level with the +, left of the
        // rail's edge; a hover's preview until a press pins it (`pinned`).
        {...(card.pop
          ? {
              popover: {
                anchor: popAnchor,
                // WINDOW A'S SIZE (owner, 2026-11-07): the remembered
                // `canvasRailPeek`, else 420×640 — clamped inside the window
                // by the chassis, the form scrolling inside it.
                size: railCardSize(getUiLayout().canvasRailPeek ?? null),
                // …AND RESIZED LIKE IT (`savePopSize`): the grips on a
                // pinned card only — a hover's preview is not a thing to
                // drag.
                ...(card.pinned
                  ? {
                      resize: {
                        minWidth: PEEK_MIN_WIDTH,
                        minHeight: PEEK_MIN_HEIGHT,
                        maxWidth: PEEK_MAX_WIDTH,
                        // Never over window B's card under it (RailPeek's
                        // floor, rail-peek-lib `bottomClear`).
                        maxBottom: (box) => bottomClear(box, favBox),
                        // …and inside the workspace row (RailPeek's own
                        // bounds), never over the sidebar or the header.
                        bounds: () => rowOf(railRef.current)?.getBoundingClientRect() ?? null,
                        onResize: savePopSize,
                        // THE WIDTH MAGNET (card-snap.ts): to window B's card.
                        adjust: (next, edge, start) => {
                          const row = rowOf(railRef.current)?.getBoundingClientRect();
                          const s = snapWidth(next, edge, favBox, PEEK_MIN_WIDTH, row ? dragMaxWidth(start, row) : PEEK_MAX_WIDTH);
                          setSnapGuide(s.guide);
                          return s.box;
                        },
                        onDragEnd: () => setSnapGuide(null),
                      },
                    }
                  : {}),
                ...(card.door && card.door !== PLUS_DOOR ? { anchorBox: draftCardBox } : {}),
                place: popPlace,
                // Window A's box, for window B's grips to snap to.
                onBox: publishPlusBox,
                // Per rail (a hop re-places the card beside the new rail).
                placeKey: popPlaceKey,
                // The row reshaping (the sidebar collapsing) re-places it.
                watch: () => rowOf(railRef.current),
                beside: railRef,
                closeSignal: cardCloseSignal,
                pinned: !!card.pinned,
                onPin: () => plusIntent.pinInside(),
                onPointerEnter: () => plusIntent.enterCard(),
                onPointerLeave: () => plusIntent.leaveCard(),
              },
            }
          : {})}
      />
  );
  const railToken = useRef({});
  useLayoutEffect(() => {
    if (hosted) publishPlusRender(railToken.current, renderCard as (c: unknown) => ReactNode);
  });
  const hostedRef = useRef(hosted);
  hostedRef.current = hosted;
  useEffect(() => {
    const id = railToken.current;
    return () => {
      withdrawPlusRender(id);
      // NO HOST (an embedded view): the card was this rail's to draw, and
      // goes with it — the next rail would remount it from its first seed and
      // a second draft would follow (CodeRabbit on #2220). Its own unmount
      // flushes the words to its draft, as any close does.
      if (!hostedRef.current) setCard(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <aside
      ref={railRef}
      className={
        "claude-rail" +
        (favs.length + bots.length > 0 ? " has-favs" : "") +
        // No glyph above the + (beside the open pane, the fallback): nothing
        // keeps its room (claude-rail.css).
        (favsOn && !chrome && !navDoors ? " no-glyph" : "") +
        (favsOn ? " is-scroll" : "") +
        (navDoors ? " has-nav" : "") +
        (favouritesOnly ? " is-fallback" : "")
      }
      aria-label="Claude"
      hidden={!shown || undefined}
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
      {chrome && (
        <div
          className="claude-rail-seam"
          role="separator"
          aria-orientation="vertical"
          aria-label="Drag to show the Claude panel"
          onPointerDown={onPullStart}
        />
      )}
      {/* THE NAV DOORS' TOP (`navDoors`): Search, Bots, Tasks and the +, in
          that order, where the panel glyph stood — on every rail, always; no
          hairline between them (owner, 2026-11-08). */}
      {navDoors && (
        <div className="claude-rail-nav">
          {/* THE SEARCH DOOR (owner, 2026-11-08): the app's launcher — the
              ⌥Space search panel over the apps and bots — summoned from the
              rail's top. Only where the app has one (`searchDoor`). */}
          {searchDoor && (
            <button
              type="button"
              className={"sidebar-rail-btn claude-rail-item" + doorOpen(launcher.open, true)}
              title="Search"
              aria-label="Search"
              onClick={() => {
                hideHint();
                launcher.opened();
                // Where the shortcut opens it, for now (owner, 2026-11-08: "just
                // open it where we default open it"); the beside-the-rail place
                // (`showLauncher({left, top})`, launcher.beside_frame) is a
                // follow-up. A miss (the panel went with the preference) is
                // nothing to show: the door stands only while the config says
                // it is there.
                showLauncher().catch(() => launcher.failed());
              }}
            >
              <Search size={16} aria-hidden="true" />
            </button>
          )}
          {/* THE BOTS DOOR (owner, 2026-11-08: "search, bots, tasks, +"): the
              Bots page, the left sidebar's row moved here; its pinned bots
              stay at the rail's foot (window B's). */}
          {botsDoor && (
            <a
              ref={botsDoorRef}
              href="/bots"
              className={"sidebar-rail-btn claude-rail-item" + doorOpen(botsUp, botsCard.pinned)}
              title={botsTip ? `Bots — ${botsTip}` : "Bots"}
              aria-label="Bots"
              {...(botsHere ? { "aria-current": "page" as const } : {})}
              {...(botsCard.ready
                ? { "aria-haspopup": "dialog" as const, "aria-expanded": botsUp, "aria-controls": BOTS_PEEK_ID }
                : {})}
              onClick={pressBotsDoor}
              onPointerEnter={(e) => {
                if (e.pointerType !== "touch" && !dragging(e) && botsCard.ready && aHovers()) botsCardIntent().enterRing(BOTS_CARD_KEY, "pointer");
              }}
              onPointerLeave={() => botsCardIntent().leaveRing("pointer")}
              onKeyDown={(e) => {
                // Escape lets a PREVIEW go (a pinned card closes by its ×, or
                // this door's press).
                if (e.key === "Escape" && botsUp && !botsCard.pinned) {
                  e.preventDefault();
                  closeBotsCard(true);
                }
              }}
            >
              {BOTS_ICON}
              {botsDot && <span className={`sidebar-rail-dot ${botsDot}`} aria-hidden="true" />}
            </a>
          )}
          <a
            ref={tasksDoorRef}
            href="/tasks"
            className={"sidebar-rail-btn claude-rail-item" + doorOpen(tasksUp, tasksCard.pinned)}
            title={tasksTip ? `Tasks — ${tasksTip}` : "Tasks"}
            aria-label="Tasks"
            {...(tasksHere ? { "aria-current": "page" as const } : {})}
            {...(tasksCard.ready
              ? { "aria-haspopup": "dialog" as const, "aria-expanded": tasksUp, "aria-controls": TASKS_PEEK_ID }
              : {})}
            onClick={pressTasksDoor}
            onPointerEnter={(e) => {
              if (e.pointerType !== "touch" && !dragging(e) && tasksCard.ready && aHovers()) tasksCardIntent().enterRing(TASKS_CARD_KEY, "pointer");
            }}
            onPointerLeave={() => tasksCardIntent().leaveRing("pointer")}
            onKeyDown={(e) => {
              // Escape lets a PREVIEW go (a pinned card closes by its ×, or
              // this door's press).
              if (e.key === "Escape" && tasksUp && !tasksCard.pinned) {
                e.preventDefault();
                closeTasksCard(true);
              }
            }}
          >
            {TASKS_ICON}
            {tasksDot && <span className={`sidebar-rail-dot ${tasksDot}`} aria-hidden="true" />}
          </a>
          <button
            ref={plusRef}
            type="button"
            className={
              "sidebar-rail-btn claude-rail-item" + doorOpen(!!card?.pop && card.door === PLUS_DOOR, !!card?.pinned)
            }
            title={plusLabel}
            aria-label={plusLabel}
            aria-haspopup="dialog"
            aria-expanded={!!card?.pop}
            aria-busy={opening || waiting !== null || undefined}
            // Never held while the card's read is out: a press during a
            // hover's read is the pin (plus-pop-lib.ts), and a disabled
            // button would swallow it.
            disabled={waiting !== null}
            onClick={openNewTask}
            onPointerEnter={(e) => {
              if (e.pointerType !== "touch" && !dragging(e) && plusHovers()) plusIntent.enterPlus();
            }}
            onPointerLeave={() => plusIntent.leavePlus()}
          >
            <Plus size={16} aria-hidden="true" />
          </button>
        </div>
      )}
      {chrome && !navDoors && (
        <button
          type="button"
          className="sidebar-rail-btn"
          title="Show the Claude panel"
          aria-label="Show the Claude panel"
          aria-expanded={false}
          // The pane's HOME (its Recent chats), never a chat (owner,
          // 2026-11-05): `onNewChat` where the page has it, after the open.
          onClick={() => {
            onOpen();
            onNewChat?.();
          }}
        >
          <PanelIcon side="right" />
        </button>
      )}
      <div className="sidebar-rail-items">
        {/* THE +: a page rail's only (claude-rail-lib `plusShown`). */}
        {!navDoors && plusShown({ favouritesOnly }) && (
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
        )}
        {anyRing && !hideList && (
          <div
            ref={listRef}
            className="claude-rail-chats"
            role="list"
            aria-label={`Chats on ${node ? name : scope}${allSeats.length > baseSeats.length ? ` (${baseSeats.length} of ${allSeats.length} shown)` : ""}`}
            // AT MOST TEN RINGS TALL, then it scrolls (claude-rail-lib
            // `LIST_ROWS_MAX`, claude-rail.css `.is-scroll`); the fades at
            // its edges say there is more above / below (`listEdges`).
            style={
              {
                "--list-rows": LIST_ROWS_MAX,
                "--fade-top": listEdges.up ? "12px" : "0px",
                "--fade-bottom": listEdges.down ? "12px" : "0px",
              } as CSSProperties
            }
            onScroll={measureEdges}
          >
            {baseSeats.map((seat, i) => (
              <Fragment key={seat.pulse.key}>
                {/* THE NODE'S CHATS, THEN A HAIRLINE, THEN THE REST faded
                    (claude-rail-lib `railRows`; owner, 2026-11-10). */}
                {seat.off && i > 0 && !baseSeats[i - 1].off && (
                  <span className="claude-rail-node-sep" aria-hidden="true" />
                )}
                {ringOf(seat)}
              </Fragment>
            ))}
          </div>
        )}
        {navDoors && gitRepo && (
          // THE GIT DOOR (owner, 2026-11-10): under the rings, past a hairline
          // always drawn while it stands (it parts Git from the nav doors when
          // there are no rings), one seat — the Git card's door (GitPeek),
          // wearing the push / pull dots (git-dock-lib `gitDoorDots`).
          <>
            <span className="sidebar-rail-sep is-tight" aria-hidden="true" />
            <div className="claude-rail-nav claude-rail-git-door">
              <button
                ref={gitDoorRef}
                type="button"
                className={"sidebar-rail-btn claude-rail-item" + doorOpen(gitUp, gitCard.pinned)}
                title={gitDoorTitle(gitRepo)}
                aria-label={gitLabel}
                {...(gitCard.ready
                  ? { "aria-haspopup": "dialog" as const, "aria-expanded": gitUp, "aria-controls": GIT_PEEK_ID }
                  : {})}
                onClick={pressGitDoor}
                onPointerEnter={(e) => {
                  if (e.pointerType !== "touch" && !dragging(e) && gitCard.ready && aHovers()) {
                    setGitCardRail(railRef.current);
                    gitCardIntent().enterRing(GIT_CARD_KEY, "pointer");
                  }
                }}
                onPointerLeave={() => gitCardIntent().leaveRing("pointer")}
                onKeyDown={(e) => {
                  // Escape lets a PREVIEW go (a pinned card closes by its ×, or
                  // this door's press).
                  if (e.key === "Escape" && gitUp && !gitCard.pinned) {
                    e.preventDefault();
                    closeGitCard(true);
                  }
                }}
              >
                {GIT_ICON}
                {gitDots?.top && <span className={`sidebar-rail-dot is-${gitDots.top}`} aria-hidden="true" />}
                {gitDots?.second && <span className={`sidebar-rail-dot is-${gitDots.second} is-second`} aria-hidden="true" />}
              </button>
            </div>
          </>
        )}
        {!navDoors && git && railGit.repo && (
          // THE GIT SECTION (header), off with the nav doors — the Git door
          // above draws it then (rail-git-store.ts, GitPeek): past the left rail's own separator
          // (sidebar.css `.sidebar-rail-sep`), the repository the page sits
          // in. Each seat wears the app's hint (platform/lib/hints), beside
          // the rail like the rings' — a 48px column has no room for words.
          <>
            <span className="sidebar-rail-sep is-tight" aria-hidden="true" />
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
                    {/* Three seats: the read asks for a card's worth
                        (rail-git.ts `RAIL_GIT_COMMITS`). */}
                    {r.commits.slice(0, 3).map((c) => {
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
        {/* THE ROOM (header, `fit`): the column's leftover, measured, never
            drawn in. UNDER the Git section, above the favourites (owner,
            2026-11-04: "Git should move up right after the list; favourites
            stay at the bottom"): the foot keeps to the list's end, Git to
            the foot, and the favourites to the rail's foot — where their
            card anchors. With favourites off, Git simply follows the list.
            The list's rings plus this box are still what the list could
            grow into: Git between them is fixed height. */}
        <div ref={roomRef} className="claude-rail-room" aria-hidden="true" />
        {favs.length > 0 && (
          // THE FAVOURITES, LAST (owner, 2026-11-03: "favourite should be
          // below git"): past their own hairline, one ring per starred chat
          // from anywhere — the chat the shell's favourite card stands
          // beside (`.is-open`). A ring here hovers like a list ring, bar the
          // open one (its caption stays); its press opens the favourite's
          // card. THE BLOCKED STAND AT ITS HEAD (owner, 2026-11-05), as list
          // rings: window A's hover, no star.
          <>
            {/* The fallback's favourites are all it draws: nothing above to
                part them from. */}
            {!favouritesOnly && <span className="sidebar-rail-sep is-tight" aria-hidden="true" />}
            <div
              className="claude-rail-favs"
              role="list"
              aria-label={blockedCount > 0 ? "Blocked and favourite chats" : "Favourite chats"}
              // Its floor (claude-rail.css): three rings, or every star when fewer.
              style={{ "--fav-floor": Math.min(favs.length, 3) } as CSSProperties}
            >
              {favs.map((seat) => ringOf(seat, true))}
            </div>
          </>
        )}
        {bots.length > 0 && (
          // THE PINNED BOTS, UNDER THE FAVOURITES (owner, 2026-11-05: "have
          // the right sidebar go all the way and have bots below it — pinned
          // bots show up there; for now I can click on it to navigate to the
          // page"): each bot's face, its name the caption, the bot's page
          // its door. The red dot is the Bots row's (GlobalSidebar): the bot
          // waits on the reader; the green one, news past their mark.
          <>
            {(favs.length > 0 || !favouritesOnly) && (
              <span className="sidebar-rail-sep is-tight is-over-faces" aria-hidden="true" />
            )}
            <div className="claude-rail-bots" role="list" aria-label="Pinned bots">
              {bots.map((b) => {
                const name = b.name || b.id;
                const href = botHref(b.id);
                const subject = { id: b.id, name, face: (b.face ?? null) as FaceSubject["face"], status: b.status as FaceSubject["status"] };
                const waiting = b.status === "waiting";
                const unread = botHasUnread(b);
                // THE RING'S CARD (BotPeek; claude-rail-lib `botRingRoute`):
                // a hover previews, a press pins — or, on the Bots page, with
                // the card off or no room for one, the ring is the page's
                // door as it was. While the card can stand it is the ring's
                // caption (an empty hint is the opt-out, as the chats').
                const card = botRingRoute({ peekOn, noRoom: botNoRoom }) === "card";
                // ONE WINDOW B (header): with a favourite's card open the
                // ring's hover is its caption, as a favourite's ring beside
                // an open favourite; a press still takes the window. While a
                // bot's card is PINNED the other bots' rings say their name
                // too — their hover shows no card (`botIntent` sits still for
                // a held card; Bugbot on #2201).
                const hovers = card && openFavKey === null && !(botPinned && botKey !== null && botKey !== b.id);
                const peeked = card && botKey === b.id;
                return (
                  <div key={b.id} role="listitem" className="claude-rail-seat">
                    <a
                      ref={(el) => {
                        if (el) {
                          botEls.current.set(b.id, el);
                          setBotRing(b.id, el);
                        } else {
                          const was = botEls.current.get(b.id);
                          botEls.current.delete(b.id);
                          if (was) clearBotRing(b.id, was);
                        }
                      }}
                      href={href}
                      data-bot-id={b.id}
                      className={
                        "sidebar-rail-btn claude-rail-item claude-rail-bot" +
                        (waiting ? " is-waiting" : "") +
                        (peeked ? " peeked" : "") +
                        (peeked && botPinned ? " is-pinned" : "")
                      }
                      aria-label={`${name} — ${botStatusWords(b.status)}`}
                      data-hint={hovers ? "" : name}
                      data-hint-title={name}
                      data-hint-side="left"
                      {...(card
                        ? {
                            "aria-haspopup": "dialog" as const,
                            "aria-expanded": peeked,
                            "aria-keyshortcuts": "ArrowLeft",
                            ...(peeked ? { "aria-controls": BOT_PEEK_ID } : {}),
                            onKeyDown: onBotRingKey(b.id),
                            // A leave is always heard (the chats' rule): the
                            // intent must not think the pointer still rests
                            // on a ring it left.
                            onPointerLeave: () => botIntent.leaveRing("pointer"),
                            onBlur: () => botIntent.leaveRing("focus"),
                          }
                        : {})}
                      {...(hovers
                        ? {
                            onPointerEnter: (e: ReactPointerEvent<HTMLAnchorElement>) => enterRing(b.id, e, botIntent),
                            onFocus: (e: ReactFocusEvent<HTMLAnchorElement>) =>
                              takeQuietFocus(e.currentTarget) ? botIntent.quietFocus(b.id) : botIntent.enterRing(b.id, "focus"),
                          }
                        : {})}
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                        e.preventDefault();
                        if (!card) {
                          hideHint();
                          void navigateUrl(href);
                          return;
                        }
                        pressBotRing(b.id, href);
                      }}
                    >
                      <span className="claude-rail-bot-face" aria-hidden="true">
                        <Face b={subject} />
                      </span>
                      {waiting ? (
                        <span className="sidebar-rail-dot is-attention" aria-hidden="true" />
                      ) : unread ? (
                        <span className="sidebar-rail-dot is-unread" aria-hidden="true" />
                      ) : null}
                    </a>
                  </div>
                );
              })}
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
          coneOf={coneOf}
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
          pageRoot={favouritesOnly ? null : root}
          onChipLeave={onChipLeave}
          held={held}
          onHeld={setHeld}
          onStarred={onStarred}
          avoid={favBox}
          oneSize={favsOn}
          pinned={pinned}
          onPin={() => {
            const key = intent.current;
            if (!key) return;
            intent.press(key);
            if (seats.find((s) => s.pulse.key === key)?.pulse.session_id) focusComposerSoon(key);
          }}
        />
      )}
      {!hosted && card && renderCard(card)}
    </aside>
  );
}
