// ONE TASK HEAD, EVERYWHERE (design.md, "One task head, everywhere"; Akshil,
// 2026-11-10). The line across the top of every view that shows ONE task's
// chat — the Tasks page's side panel, the rail's Tasks card, the New task card
// once it has a chat open, the Cards wall's popup — and the Tasks card's list
// head on the same chassis.
//
// WHY ONE COMPONENT. "Tasks" was drawn by nine separate heads, and the same
// task wore a different one depending on the door the reader came in by: the
// star missing in one, prev/next in two, a ⋮ with its own item list in a third,
// "Open" spelled five ways, the number printed twice, three heights. Every one
// of those was a head that had drifted from its siblings, and the only way a
// set of heads stops drifting is to stop being a set. This file is built
// entirely from pieces that already existed — `TaskPeekWho` (identity and
// rename), the chat's `Kebab` (the host hands it in), `useStripFit`
// (row-fit.ts) — so nothing here is new vocabulary, only one arrangement of it.
//
// THE CANONICAL ORDER, left to right, on one 44px row (`--task-head-h`) — the
// Tasks row's own line with a lead and a ⋮ (Akshil, 2026-11-15, "a/b slots"):
//
//   [lead] [↑][↓] │ status · agent · [folder] · ID SLOT · title ✎ · reply
//                 │ [queue] [status word] … [chips…] [seats…] [where]
//                 │ COUNT SLOT TIME SLOT [⋮]
//
//   * LEAD — exactly one corner control, the same 28px icon button either way:
//     × closes the container the reader opened (panel, card, dialog); ← goes
//     back to the list INSIDE the same container. No word beside the arrow —
//     the hint carries it ("Back to tasks").
//   * ↑↓ — only where there is a list behind the head to walk.
//   * STATUS — the ring, or the face it wears; pressed, it opens the icon
//     picker (TaskIconPick — any task, 2026-11-16). It swaps with nothing.
//   * FOLDER — where the task lives: the host's `place`, or the folder chip.
//   * WHERE — where IN the place (2026-11-16): Canvas, the node, the file, as
//     an icon and its word, right before the count slot; the icon alone as
//     soon as the row is short (rung 1).
//   * THREE A/B SLOTS, one box and two occupants each, so nothing moves: `a`
//     at rest, `b` while the pointer is on the head (or the keyboard is on a
//     `b` control; always, where there is no hover):
//       ID     the number        ↔ Archive / Unarchive (TaskFileSeat)
//       COUNT  the message count ↔ the favourite star
//       TIME   when it last ran  ↔ ↗ Open
//     A slot with no `b` (a run in flight has nothing to file; a pane IS the
//     page, so it has no Open; favourites off) keeps its `a` under the pointer.
//   * a host's `subtitle` (the rail card's hover preview: the last reply)
//     trails the title, muted, and gives way before it; the queue caption and
//     the paused word after them. The head prints no reply of its own.
//   * CHIPS (worktree, PR), host SEATS, and the ⋮ at the end.
//
// NEVER MOVED, NEVER HIDDEN: the lead, the folder, and the ⋮. (↗ Open stood
// beside the ⋮ at every width from 2026-11-10; the 2026-11-15 ruling replaced
// that — it is the time slot's `b` now, as on a row.) Only the title flexes;
// when even a floored title will not fit, the rest gives in a measured order
// — never a breakpoint (row-fit.ts `TASK_HEAD_DROPS`, `data-fit` below),
// cumulatively:
//
//   1. the time: the slot shrinks to a glyph, and Open under the pointer is
//      the ↗ alone; 2. the chips; 3. the host's seats; 4. the ↑↓ chevrons;
//   5. the id slot, the title, its line and the trail take row 2 — row 1
//      keeps the lead, status, folder … where, count slot, time slot, ⋮
//      (`stacked: "auto"`; the number goes down with the title it names,
//      Akshil, 2026-11-16);
//   6. the id slot (Archive stays in the ⋮).
//
// A RAIL WINDOW IS TWO LINES, ALWAYS (`reveal="window"` with a task; Akshil,
// 2026-11-16: "in rail every task layout is a simple two-liner") — the hover
// / favourite card, the Tasks card's chat, the New task card's chat, at every
// width, not only when the ladder runs short:
//
//   [pin][←]  folder › file ………… [chips] COUNT · TIME  [⋮]
//   status · ID SLOT · title ✎ · reply · [queue][status word]
//
// Row 1 is where the task lives (the place, a chevron, where in it) and its
// facts; row 2 is the task. The three slots trade as everywhere, and nothing
// folds to an icon: the where-tag and the door keep their words, and a long
// place gives width instead (task-head.css `.is-two-line`). THE ⋮ COMES WITH
// THE STAR AND THE DOOR (Akshil, 2026-11-17: "show the kebab when we show fav
// and open icon"): it sits in the acts' layer, so the count and the time end
// the row at rest and the pointer on the card brings star · Open · ⋮ in the
// same box. A panel or a pane keeps the one row above — unless its host asks
// for the same two lines (`twoLine`, 2026-11-17: the Tasks page's panel and
// the chat panes), keeping its own reveal and its ⋮ always standing.
//
// WHAT THE HOST OWNS. The ⋮ is the host's `<Kebab …/>` handed in whole, because
// what it does on Archive and Delete (advance to the next task, go back to the
// list, close the dialog) is the host's business; the head only seats it. The
// same for `chips` and `seats`.
import { useCallback, useEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronRight, ChevronUp, MessageSquare, X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { queueCaption, type QueueFacts } from "@platform/lib/queue";
import { TaskStar } from "@platform/ui/TaskStar";
import { IdentityChip } from "./ScheduleTaskViews";
import { QueuedCaption, TaskPeekWho } from "./TaskPeekWho";
import { TaskFileSeat, type FiledKind } from "./TaskFileSeat";
import { basename, taskColumn, taskWhen } from "./tasks-lib";
import {
  TASK_HEAD_DROPS,
  TASK_HEAD_PANEL_DROPS,
  TASK_HEAD_PANEL_FIT,
  TASK_HEAD_STACK_LEVEL,
  taskHeadNeed,
  useStripFit,
} from "./row-fit";

export interface TaskHeadLead {
  kind: "close" | "back";
  /** The button's accessible name ("Close the task panel", "Back to tasks"). */
  label: string;
  /** The tooltip, when it says more than the name ("Close · Esc"). */
  hint?: string;
  onPress(): void;
  ref?: Ref<HTMLButtonElement>;
  disabled?: boolean;
  /** Why it is disabled — said in the hint and the name, since a disabled
   *  button's tooltip is the one place a reader looks. */
  reason?: string;
  /** The next press does it (a dirty dialog's second ✕): `is-armed`. */
  armed?: boolean;
}

export interface TaskHeadNav {
  prev: boolean;
  next: boolean;
  step(delta: -1 | 1): void;
  /** Append the keys ("· ↑" / "· ↓") to the hints — where the arrow keys
   *  really do walk the list (the Tasks page's panel). */
  keyHints?: boolean;
}

export interface TaskHeadOpen {
  href: string;
  /** The door's accessible name and hint ("Open in Explorer", "Open the Tasks
   *  page"). */
  label: string;
  /** The word after the ↗ — "Open", or "Open tasks" on the card's list head. */
  word?: string;
  /** The ↗ itself (default on). Off where the door does not leave the page
   *  on screen (the rail card, #2218's rule). */
  arrow?: boolean;
  /** NO CAPTION on the door (Akshil, 2026-11-18, of a rail window's Open:
   *  "remove this tooltip from open"): its word says what it does, and the
   *  panel hung over the head it was pressed in. The rail's windows. The
   *  accessible name stays (`label`). */
  quiet?: boolean;
  onOpen(href: string): void | boolean | Promise<boolean | void>;
}

/** A queued chat's facts — the row's own, so the identity can wear the queued
 *  row's number, title and dashed ring (Topbar's rule). */
export type TaskHeadQueue = (QueueFacts & Pick<Partial<Task>, "task_id" | "title">) | null;

/** The count slot's `a`: how many messages the task holds. */
export interface TaskHeadCount {
  n: number;
  /** Replies not read yet — the count turns to ink and the hint says so. */
  unread?: number;
}

/** The time slot's `a`: when the task last moved ("16d ago"), and which
 *  moment that is in full ("Last run 10/26/2026, 12:01:08"). */
export interface TaskHeadWhen {
  text: string;
  title?: string;
}

export interface TaskHeadProps {
  lead: TaskHeadLead | null;
  /** A host-built corner control, drawn in the lead's seat INSTEAD of
   *  `lead` (the rail's favourite card: its pin/× button keeps its own
   *  behaviour). */
  leadNode?: ReactNode;
  /** A control BEFORE the lead, in the same cluster: the canvas and app
   *  panes' hide-the-panel × while a chat's ← holds the lead — those panes
   *  have no other close. Order: [preLead][lead][↑][↓]. */
  preLead?: ReactNode;
  /** THE RAIL CARD'S PIN (claude-rail RailPinButton; owner, 2026-11-16): a
   *  rail card's head only. Seated first, where the × was — before a ← — so the
   *  corner reads `[pin] [←] [↑][↓]`; in the acts cluster, which no rung
   *  of the ladder folds. */
  pinNode?: ReactNode;
  /** The favourite star, the count slot's `b` (default on). Off where the
   *  favourites switch is off (the rail card's legacy rule): the count stays.
   *  A `twoLine` host MUST say so: `TaskStar` draws nothing while the switch
   *  is off, but its element still counts as an act, and a head that pairs
   *  its facts would trade the count for nothing under the pointer. */
  star?: boolean;
  /** A host's own star control in the star's seat. */
  starNode?: ReactNode;
  /** A host's own filing button, the id slot's `b` in place of the head's
   *  `TaskFileSeat` (the rail card's Archive, which tells its rail). The name
   *  is from when it stood over the status ring. */
  statusOverlay?: ReactNode;
  /** The count slot's `a`. Omitted: the task's own `message_count` (the List
   *  row's floor of one). `null`: no count (a draft, a pulse-only row). */
  count?: TaskHeadCount | null;
  /** The time slot's `a`. Omitted: `taskWhen(task)`, the List row's cell.
   *  `null`: no time. */
  when?: TaskHeadWhen | null;
  /** Ring facts only the host knows (TaskPeekWho `ring`): unread, held
   *  draft, failed. */
  ring?: { unread?: boolean; draftHeld?: boolean; failed?: boolean };
  /** A muted line after the title, in the title's row (TaskPeekWho). None
   *  unless the host passes one: the head never prints the task's last reply
   *  itself (2026-11-16). */
  subtitle?: ReactNode;
  /** After a rename the server took (TaskPeekWho `onRenamed`). */
  onRenamed?: () => void;
  /** The node the host's seats sit in (`.task-head-seats`) — the chat
   *  measures it to fold its seat labels (`useFitStrip`). */
  seatsRef?: (el: HTMLElement | null) => void;
  /** THE ID SLOT FILES THE TASK (TaskFileSeat): on by default for any task
   *  `filingIntent` can file — Archive / Unarchive takes the number's place
   *  while the head is pointed at. A host's own `statusOverlay` wins; `false`
   *  turns it off (a host whose archive lives elsewhere). */
  fileSeat?: boolean;
  /** After the seat filed the task: the host does what its ⋮ does on Archive
   *  (advance, go back, close); an Unarchive keeps the reader in place. */
  onFiled?: (kind: FiledKind) => void;
  /** The group's accessible name (default "Task header"). */
  ariaLabel?: string;
  nav?: TaskHeadNav;
  /** Identity: `<TaskPeekWho star …>`. Null for a head with no task (yet). */
  task: Task | null;
  /** A static title when `task` is null (the card list head's "Tasks"). */
  title?: ReactNode;
  /** An `id` on the title element, for a `Modal`'s `aria-labelledby` — and the
   *  title then wears the heading role the chassis' `h2` used to carry. */
  titleId?: string;
  running?: boolean;
  /** Nobody has answered for this session yet: a placeholder holds the seat. */
  pending?: boolean;
  queue?: TaskHeadQueue;
  /** "paused · resumes 4:00 AM" (platform/lib/usage-limit). */
  status?: string;
  /** WHERE THE TASK LIVES, before the number. Omitted: the task's folder
   *  chip. `null`: none — a head inside a view already scoped to that folder
   *  (the Explorer pane over the same folder). */
  place?: ReactNode;
  /** WHERE IN THE PLACE (Akshil, 2026-11-16): the canvas chat's Canvas or
   *  node tag, a folder chat's file tag (PlaceTags `part="where"`) — at the
   *  row's right end, just before the count slot. An icon and its word; the
   *  icon alone as soon as the row is short (the ladder's first rung,
   *  task-head.css). Not an a/b slot: it stands at rest and under the
   *  pointer. */
  where?: ReactNode;
  chips?: ReactNode;
  seats?: ReactNode;
  /** The node the chat portals its usage-limit word into (ChatMount
   *  `headStatusHost`) — beside the identity, on no rung of the ladder:
   *  "paused · resumes 4:00 AM" is the one fact the ring cannot carry. */
  statusRef?: (el: HTMLElement | null) => void;
  /** The node the chat portals its worktree menu into (ChatMount
   *  `headChipsHost`) — a seat at the head of the chips. */
  chipsRef?: (el: HTMLElement | null) => void;
  /** WHAT BRINGS THE b ITEMS (Akshil, 2026-11-16). `"window"` — a card off
   *  the rail: the pointer anywhere on the card (or the keyboard inside it)
   *  shows them, except the Archive, which waits for the pointer on the
   *  head; the facts stand at rest and the ⋮ comes with the star and the
   *  door; a head with a task is two lines at every width (where it lives
   *  and its facts, then the task). `"slot"` (default) — a panel or
   *  a pane: the pointer on the head shows them, the facts and the ⋮ stand
   *  at rest, and the head stays one row. */
  reveal?: "window" | "slot";
  /** TWO LINES IN A PANEL OR A PANE TOO (Akshil, 2026-11-17: "take a look at
   *  the two line header in popup windows a/b, try that same in expanded
   *  sidebar"): the rail window's layout — where it lives and the facts on
   *  row 1, the task on row 2 — for a head that keeps `reveal="slot"`. The
   *  host asks; a head with nothing to name (no task, no title, nobody
   *  pending) stays one row. A rail window is two lines without asking. */
  twoLine?: boolean;
  /** ↗ Open — the time slot's `b` on a task's head; always shown on a head
   *  with no task (the card's list head). Null where the head IS the page:
   *  the time stays under the pointer. */
  open?: TaskHeadOpen | null;
  /** The host's ⋮ (a `Kebab`), or a function of the ladder's fit. Null
   *  before a session exists. */
  kebab?: ReactNode | ((fit: number) => ReactNode);
  /** The title on a row of its own: `"auto"` (default) lets the ladder
   *  decide at rung 5; `true` pins that layout (the other rungs still
   *  measured); `false` never stacks and caps the ladder at rung 4. A rail
   *  window's head with a task is two lines whatever this says. */
  stacked?: boolean | "auto";
  /** Arm the fit ladder (the peek: only while the panel is up). */
  fitEnabled?: boolean;
  className?: string;
}

/** Has the ladder folded ↗ Open away? Never: it is the time slot's `b` at
 *  every width (word, then the ↗ alone), so no host's ⋮ carries it. Kept so a
 *  host that still asks gets the honest answer. */
export function foldedOpen(_fit: number): boolean {
  return false;
}

// Module constants, because `useStripFit` re-arms its observers whenever the
// ladder's identity changes.
const DROPS_ALL: readonly string[] = TASK_HEAD_DROPS;
/** `stacked: false`: the same rungs less the id slot's; the title's rung
 *  folds nothing (the component never stacks), so the ladder walks past it. */
const DROPS_NO_STACK: readonly string[] = TASK_HEAD_DROPS.slice(0, TASK_HEAD_DROPS.length - 1);
/** A two-line panel's own order: the tools' words first, then the tags. */
const DROPS_PANEL: readonly string[] = TASK_HEAD_PANEL_DROPS;

export function TaskHead({
  lead,
  nav,
  task,
  title,
  titleId,
  running = false,
  pending = false,
  queue = null,
  status = "",
  place,
  where,
  chips,
  seats,
  chipsRef,
  open = null,
  kebab = null,
  stacked = "auto",
  fitEnabled = true,
  className,
  leadNode,
  preLead,
  pinNode,
  star = true,
  starNode,
  statusOverlay,
  ring,
  subtitle,
  onRenamed,
  seatsRef,
  ariaLabel = "Task header",
  fileSeat = true,
  onFiled,
  statusRef,
  count,
  when,
  reveal = "slot",
  twoLine: twoLineAsked = false,
}: TaskHeadProps) {
  // A SPENT PRESS HANDS THE RING BACK (the Tasks row's `.is-refiled`): up on
  // the filing press, down when the pointer leaves the head — no selector can
  // say "hovered, but the hover already spent its press".
  const [refiled, setRefiled] = useState(false);
  // The title is being edited (the title half tells the marks half, which
  // holds the icon button and the id slot).
  const [renaming, setRenaming] = useState(false);
  // The icon picker is open (the marks half tells the title half, which holds
  // the id slot once the head is stacked).
  const [picking, setPicking] = useState(false);
  const isWindow = reveal === "window";
  // A RAIL WINDOW'S TASK HEAD IS ALWAYS TWO LINES (Akshil, 2026-11-16: "in
  // rail every task layout is a simple two-liner"), at every width:
  //
  //   [pin][←]  folder › file ……………… [chips] count · time  [⋮]
  //   status · id · title · reply
  //
  // Row 1 says where the task lives and its facts; row 2 is the task itself.
  // Nothing in it folds to an icon: the where-tag and the door keep their
  // words (task-head.css `.is-two-line`).
  //
  // A PANEL OR A PANE THAT ASKS (`twoLine`, 2026-11-17) wears the same two
  // lines — and keeps them for a chat with no task row yet (the fallback
  // title, the skeleton), so the head does not grow a row when the task lands.
  const twoLine = isWindow ? !!task : twoLineAsked && !!(task || title || pending);
  // …and folds in its own order (row-fit `TASK_HEAD_PANEL_DROPS`): the tools'
  // words, then the tags, then the time, then the tools.
  const panelLadder = twoLine && !isWindow;
  // Measured on the whole head — both rows — so a rung can answer either.
  const [measured, headRef] = useStripFit(
    panelLadder ? DROPS_PANEL : stacked === false ? DROPS_NO_STACK : DROPS_ALL,
    fitEnabled,
    taskHeadNeed,
    // A new title (or another task) is a new row to measure from rung 0 — and
    // so is the other ladder.
    `${task?.key ?? ""}\n${queue?.title ?? task?.title ?? ""}\n${panelLadder ? "panel" : ""}`,
  );
  // A LADDER SWITCHED OFF KEEPS ITS LAST RUNG (a rail card measures only
  // while it is up, RailPeek `fitEnabled`): the card is still on screen while
  // it fades, and dropping to rung 0 there brought the branch tag back into a
  // row with no room for it. A head that was never measured stays at 0.
  const lastLevel = useRef(0);
  if (fitEnabled) lastLevel.current = measured;
  const level = fitEnabled ? measured : lastLevel.current;
  // The one-row rung whose folds the stylesheet draws (`data-fit`).
  const fit = panelLadder ? (TASK_HEAD_PANEL_FIT[level] ?? 7) : level;
  const seatsTight = panelLadder && level >= 1;
  // THE WHOLE WINDOW IS THE TRIGGER (Akshil, 2026-11-15): the b items show
  // while the pointer is anywhere on the card the head tops — not only on the
  // 44px line — or the keyboard is inside it (2026-11-16).
  const [headEl, setHeadEl] = useState<HTMLElement | null>(null);
  const setHead = useCallback(
    (el: HTMLElement | null) => {
      headRef(el);
      setHeadEl(el);
    },
    [headRef],
  );
  const [hot, setHot] = useState(false);
  useEffect(() => {
    // (A test's stand-in node has no tree to walk and no events to hear.)
    // A two-line panel (the expanded sidebar) takes the same trigger: the
    // pointer anywhere on the pane shows its star and its door.
    if (!isWindow && !twoLine) return;
    if (!headEl || typeof headEl.closest !== "function" || typeof headEl.addEventListener !== "function") return;
    // The host's `data-task-window`, or the nearest box that holds MORE than
    // the head — a wrapper that is only the head's own line (the rail card's
    // `display: contents` seat) is not a window.
    let win = headEl.closest<HTMLElement>("[data-task-window]");
    if (!win) {
      const line = headEl.getBoundingClientRect().height;
      let up = headEl.parentElement;
      while (up && up !== document.body && up.getBoundingClientRect().height < line * 2) up = up.parentElement;
      win = up && up !== document.body ? up : headEl.parentElement;
    }
    if (!win || typeof win.addEventListener !== "function") return;
    // …OR WHILE THE CARD HAS THE KEYBOARD (Akshil, 2026-11-16: "show fav and
    // open when card is in focus as well, not just mouse in that area"): a
    // card the reader is typing in keeps its star, its door and its ⋮ up
    // wherever the pointer went. The Archive still waits for the pointer on
    // the head itself (task-head.css).
    let pointerIn = false;
    let focusIn = false;
    const sync = () => setHot(pointerIn || focusIn);
    const on = () => {
      pointerIn = true;
      sync();
    };
    // Leaving the window also re-arms a spent filing press (below).
    const off = () => {
      pointerIn = false;
      sync();
      setRefiled(false);
    };
    const focusOn = () => {
      focusIn = true;
      sync();
    };
    const focusOff = (e: FocusEvent) => {
      // A move between two things inside the card is not a leave.
      const to = e.relatedTarget as Node | null;
      focusIn = !!to && win.contains(to);
      sync();
    };
    win.addEventListener("pointerenter", on);
    win.addEventListener("pointerleave", off);
    win.addEventListener("focusin", focusOn);
    win.addEventListener("focusout", focusOff);
    // Mounted under a pointer that is already there (a card opened by a
    // press), or around the focus (its composer took the keyboard first).
    if (typeof win.matches === "function" && win.matches(":hover")) pointerIn = true;
    if (typeof win.contains === "function" && typeof document !== "undefined" && document.activeElement && win.contains(document.activeElement)) {
      focusIn = true;
    }
    sync();
    return () => {
      win.removeEventListener("pointerenter", on);
      win.removeEventListener("pointerleave", off);
      win.removeEventListener("focusin", focusOn);
      win.removeEventListener("focusout", focusOff);
      setHot(false);
    };
  }, [headEl, isWindow, twoLine]);
  // THE TITLE MOVES ROWS HERE, which remounts it: a rename half-typed when
  // the ladder crosses the stack rung loses its draft (an accepted edge case).
  // ONLY A RAIL WINDOW STACKS ON ITS OWN (Akshil, 2026-11-16: "2nd line thing
  // is only for small windows, not for expanded sidebar") — the ladder never
  // stacks a panel or a pane; one is two lines only when its host asks
  // (`twoLine`, 2026-11-17).
  const stackedNow =
    twoLine || stacked === true || (stacked === "auto" && isWindow && fit >= TASK_HEAD_STACK_LEVEL);
  /** The heading the dialog is named by, when there is a dialog (`titleId`). */
  const headingAttrs = titleId ? { id: titleId, role: "heading", "aria-level": 2 } : {};
  // A QUEUED LINE NAMES THE QUEUED ROW (Topbar's rule, Bugbot PR #1194): its
  // number, its title and its dashed ring — never its project, which may be
  // another folder's.
  const line = queue ? queueCaption(queue) : null;
  const who: Task | null =
    task && line && queue
      ? ({
          ...task,
          ...(queue.task_id ? { task_id: queue.task_id } : {}),
          ...(queue.title ? { title: queue.title } : {}),
          status: "queued",
        } as Task)
      : task;
  const kebabNode = typeof kebab === "function" ? kebab(fit) : kebab;
  const leadIcon =
    lead?.kind === "back" ? (
      <ArrowLeft size={16} strokeWidth={1.6} aria-hidden="true" />
    ) : (
      <X size={14} aria-hidden="true" />
    );
  const blockedWhy = lead?.disabled && lead.reason ? lead.reason : "";
  // THE SEAT FILES WHAT THE RING SHOWS. The page knows a turn is live before
  // the listing does (TaskPeekWho `running`): for those seconds the row still
  // says done, `filingIntent` would offer Archive with no confirm, and the
  // press would stop the turn that just started (CodeRabbit, #2230). The seat
  // reads the same effective status the ring draws — a card being answered
  // (`needs_attention`) keeps its confirmed Archive, a queued line its own.
  const liveTurn = !line && running && !!who && taskColumn(who) !== "needs_attention";
  const seatTask = who && liveTurn ? ({ ...who, status: "in_progress" } as Task) : who;
  // NO SESSION ASKED FOR (2026-11-15): filing goes by the task's key, and the
  // list row offers Archive on a task that never ran (an upcoming scheduled
  // one, `session_id: ""`). `filingIntent` alone decides — a draft, a queued
  // line and a run in flight still get no button, and the number stays.
  const idOverlay: ReactNode = statusOverlay ? (
    statusOverlay
  ) : fileSeat && seatTask ? (
    <TaskFileSeat task={seatTask} onPress={() => setRefiled(true)} {...(onFiled ? { onFiled } : {})} />
  ) : null;
  const keys = !!nav?.keyHints;
  // WHERE IT LIVES: the host's own node, or the List row's folder chip — a
  // label here, not a filter (there is no list under a head to narrow).
  const placeNode: ReactNode =
    place !== undefined ? place : who?.project ? <IdentityChip name={basename(who.project)} title={who.project} /> : null;
  const placeEl = placeNode ? <span className="task-head-place">{placeNode}</span> : null;

  // THE MARKS HALF: status, agent mark, folder, id slot. Always on row 1.
  const marks = who ? (
    <TaskPeekWho
      // Keyed on the task, under its own name: in a two-line head this half
      // and the title half are siblings on row 2.
      key={`marks:${who.key}`}
      task={who}
      running={line ? false : running}
      part="marks"
      className="task-head-marks-seat"
      iconPress
      // Two lines: the place stands on row 1, the ring goes down to row 2.
      place={twoLine ? null : placeEl}
      idOverlay={idOverlay}
      // STACKED, THE NUMBER GOES DOWN WITH THE TITLE (Akshil, 2026-11-16):
      // row 2 reads "T012 title", and its Archive trades there.
      idAt={stackedNow ? "title" : "marks"}
      editing={renaming}
      onPickingChange={setPicking}
      {...(ring ? { ring } : {})}
      {...(onRenamed ? { onRenamed } : {})}
    />
  ) : null;
  // NO REPLY LINE OF ITS OWN (Akshil, 2026-11-16): the head drew the task's
  // last reply after every title, which the heads it replaced never did —
  // only the Tasks row and the rail card's hover preview print it. A host
  // that wants a line after the title passes `subtitle` (RailPeek).
  const subtitleNode: ReactNode = subtitle ?? null;
  // THE TITLE HALF, wherever it stands: on row 1 after the marks, or —
  // stacked — on row 2 with the trail. A static title or the skeleton for a
  // head with no task.
  const identity = who ? (
    <TaskPeekWho
      // KEYED ON THE TASK: a walk to another task (⌃⇧J/K, ↑↓) under a
      // standing head drops a half-typed rename rather than saving it there.
      key={who.key}
      task={who}
      part="title"
      className="task-head-title-seat"
      idOverlay={idOverlay}
      idAt={stackedNow ? "title" : "marks"}
      picking={picking}
      onEditingChange={setRenaming}
      {...(titleId ? { titleId } : {})}
      {...(subtitleNode ? { subtitle: subtitleNode } : {})}
      {...(onRenamed ? { onRenamed } : {})}
    />
  ) : title ? (
    <span
      className="task-head-title-static task-head-title-seat"
      {...headingAttrs}
      {...(typeof title === "string" ? { title } : {})}
    >
      {title}
    </span>
  ) : pending ? (
    // NOBODY HAS ANSWERED FOR THIS SESSION YET (Topbar's `pending`): the
    // identity's silhouette — a ring-sized dot and a title's bar — so the row
    // does not resize when the real one lands.
    <span className="c-tb-skel task-head-skel" aria-hidden="true">
      <span className="c-skel-dot" />
      <span className="c-skel-bar is-title" />
    </span>
  ) : null;

  // THE COUNT SLOT: how many messages at rest, the star under the pointer.
  // The List row's count and its floor of one; nothing for a provisional row.
  const countFact: TaskHeadCount | null =
    count !== undefined
      ? count
      : who && !who.provisional && typeof who.message_count === "number"
        ? { n: Math.max(1, who.message_count) }
        : null;
  const starEl = who ? (starNode ?? (star ? <TaskStar task={who} /> : null)) : null;
  const unread = countFact?.unread ?? 0;
  const countWords = countFact ? `${countFact.n} message${countFact.n === 1 ? "" : "s"}` : "";
  /** The count as a fact: `cls` says which seat holds it. */
  const countEl = (cls: string) =>
    countFact ? (
      <span
        className={cls + " task-head-count" + (unread > 0 ? " is-unread" : "")}
        // `role="img"` so the label is read (the rail card's rule).
        role="img"
        aria-label={countWords + (unread > 0 ? `, ${unread} unread` : "")}
        data-hint={`${countWords} in this task` + (unread > 0 ? ` — ${unread} unread` : "")}
      >
        {countFact.n}
        <MessageSquare size={12} aria-hidden="true" />
      </span>
    ) : null;
  const countSlot =
    who && (countFact || starEl) ? (
      <span className="task-head-slot task-head-countslot">
        {countEl("task-head-slot-a")}
        <span className="task-head-slot-b">{starEl}</span>
      </span>
    ) : null;

  // THE TIME SLOT: when it last moved at rest, ↗ Open under the pointer —
  // whose hint then carries the time it stands over.
  const whenFact: TaskHeadWhen | null =
    when !== undefined ? when : who && !who.provisional ? taskWhen(who) : null;
  const openLink = open ? (
    // A real link with a real href, so ⌘-click opens a tab, exactly like a
    // row's own door.
    <a
      className={"task-head-open" + (open.arrow === false ? " is-wordy" : "")}
      href={open.href}
      aria-label={open.label}
      {...(open.quiet ? {} : { "data-hint": who && whenFact?.title ? `${open.label} · ${whenFact.title}` : open.label })}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        void open.onOpen(open.href);
      }}
    >
      {/* Always drawn: a door with no ↗ (`is-wordy`, #2218) hides it while its
          word stands, and wears it alone where the head is short on width. */}
      <ArrowUpRight className="task-head-open-arrow" size={13} strokeWidth={1.6} aria-hidden="true" />
      <span className="task-head-open-word">{open.word ?? "Open"}</span>
    </a>
  ) : null;
  const timeEl = (cls: string) =>
    whenFact ? (
      <span
        className={cls + " task-head-time"}
        {...(whenFact.title ? { "data-hint": whenFact.title } : {})}
      >
        {whenFact.text}
      </span>
    ) : null;
  const timeSlot = !who ? (
    // A HEAD WITH NO TASK has no a/b: its door stands as it always did.
    openLink
  ) : whenFact || openLink ? (
    <span className="task-head-slot task-head-timeslot">
      {timeEl("task-head-slot-a")}
      <span className="task-head-slot-b">{openLink}</span>
    </span>
  ) : null;
  // A RAIL WINDOW'S FACTS AND ACTS ARE TWO LAYERS OF ONE BOX (Akshil,
  // 2026-11-16: "too much space between message count and time", "star and
  // Open have a weird gap, keep them closer"). Two slots, each as wide as its
  // wider occupant, kept the row from shifting — but "1d ago" then stood in a
  // box sized for "Open ↗" and the star in one sized for the count, so each
  // pair had a hole in it. Here the count and the time sit side by side in
  // one layer, the star and the door in another, both against the box's right
  // edge; the box is the wider LAYER, so nothing outside it moves and nothing
  // inside it is spread.
  //
  // ONLY WHERE BOTH FACTS TRADE (or neither does). With one act alone
  // (favourites off: no star; a pane that is the page: no door) one fact
  // STAYS under the pointer, and a stayer drawn in two independently aligned
  // layers jumped by the difference of their widths (Bugbot on #2297). Those
  // heads keep one slot per fact: the stayer is one node that never moves,
  // and the other trades in a box of its own.
  const hasActs = !!(starEl || openLink);
  const clustered = (isWindow || twoLine) && !!who && (!hasActs || (!!starEl && !!openLink));
  // THE ⋮ IS ONE OF THE ACTS in a rail window's two-line head (Akshil,
  // 2026-11-17), seated after the star and the door (`actsBox`). A PANEL OR
  // A PANE in two lines keeps its ⋮ standing at the row's end, as on its one
  // row.
  const kebabEl = kebabNode ? <span className="task-head-kebab">{kebabNode}</span> : null;
  // A RAIL WINDOW'S ACTS AND FACTS TAKE A ROW EACH (Akshil, 2026-11-22: "move
  // the message count and time to the 2nd row and always show fav and open
  // button on right on 1st row … msg count and time will be shown in right
  // side of 2nd row"). Row 1 ends in star · Open · ⋮, standing at rest too;
  // row 2 ends in count · time after the title. Nothing trades any more, so
  // nothing slides — with one act or none as well. Every two-line head: the
  // rail's windows, and the side panel and the Tasks page panel that wear
  // the same two lines (Akshil, 2026-11-22: "these look good").
  const splitRows = twoLine && !!who;
  const kebabInActs = splitRows && !!kebabEl;
  const rowFacts =
    splitRows && (countFact || whenFact) ? (
      <span className="task-head-rowfacts">
        {countEl("task-head-fact")}
        {timeEl("task-head-fact")}
      </span>
    ) : null;
  const actsBox =
    splitRows && (hasActs || kebabEl) ? (
      <span className="task-head-facts has-acts is-split">
        <span className="task-head-facts-layer is-b">
          {starEl}
          {openLink}
          {kebabEl}
        </span>
      </span>
    ) : null;
  const factsCluster =
    clustered && (countFact || whenFact || hasActs) ? (
      <span className={"task-head-facts" + (hasActs ? " has-acts" : "")}>
        {countFact || whenFact ? (
          <span className="task-head-facts-layer is-a">
            {countEl("task-head-fact")}
            {timeEl("task-head-fact")}
          </span>
        ) : null}
        {hasActs ? (
          <span className="task-head-facts-layer is-b">
            {starEl}
            {openLink}
          </span>
        ) : null}
      </span>
    ) : null;
  // The queue caption and the paused word trail the title, on its row — and
  // the host's status seat (`statusRef`, the chat's portaled pause word) with
  // them: on NO rung, and onto row 2 with the title when stacked.
  const trail = (
    <>
      {line ? <QueuedCaption line={line} /> : null}
      {status ? (
        <span className="task-head-status" aria-live="polite">
          {status}
        </span>
      ) : null}
      {statusRef ? <span className="task-head-status-host" ref={statusRef} /> : null}
    </>
  );

  return (
    // A GROUP, not a <header>: a head inside a panel, a card or a dialog is
    // not the page's banner, and several of them stood on one page as several
    // banner landmarks.
    <div
      ref={setHead}
      className={
        "task-head" +
        (stackedNow ? " is-stacked" : "") +
        (isWindow ? " is-window" : "") +
        (twoLine ? " is-two-line" : "") +
        (seatsTight ? " is-seats-tight" : "") +
        (hot ? " is-hot" : "") +
        (refiled ? " is-refiled" : "") +
        (className ? ` ${className}` : "")
      }
      // The press is spent until the pointer leaves the ID SLOT (the seat it
      // was made in, task-head.css) — leaving the head covers a pointer that
      // left the window through it.
      onMouseLeave={refiled && !isWindow ? () => setRefiled(false) : undefined}
      onMouseOver={
        refiled && !isWindow
          ? (e) => {
              const at = e.target as Element | null;
              if (!at?.closest?.(".task-head-idslot")) setRefiled(false);
            }
          : undefined
      }
      role="group"
      aria-label={ariaLabel}
      data-fit={fit}
    >
      <div className="task-head-row">
        {preLead || leadNode || lead || nav || pinNode ? (
          <div className="task-head-acts">
            {preLead}
            {/* A ← lead: the pin before it, after any × (`pinNode`). */}
            {lead?.kind === "back" && !leadNode ? pinNode : null}
            {leadNode ? (
              leadNode
            ) : lead ? (
              <button
                ref={lead.ref}
                type="button"
                className={`task-head-btn task-head-lead is-${lead.kind}` + (lead.armed ? " is-armed" : "")}
                aria-label={blockedWhy ? `${lead.label} — ${blockedWhy}` : lead.label}
                data-hint={blockedWhy || lead.hint || lead.label}
                disabled={lead.disabled}
                onClick={() => lead.onPress()}
              >
                {leadIcon}
              </button>
            ) : null}
            {/* A × lead (or none): the pin after it (`pinNode`). */}
            {lead?.kind === "back" && !leadNode ? null : pinNode}
            {nav ? (
              // One seat for both chevrons, so one rung folds them (3).
              <span className="task-head-nav">
                <button
                  type="button"
                  className="task-head-btn"
                  aria-label="Previous task"
                  data-hint={keys ? "Previous task · ↑" : "Previous task"}
                  disabled={!nav.prev}
                  onClick={() => nav.step(-1)}
                >
                  <ChevronUp size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="task-head-btn"
                  aria-label="Next task"
                  data-hint={keys ? "Next task · ↓" : "Next task"}
                  disabled={!nav.next}
                  onClick={() => nav.step(1)}
                >
                  <ChevronDown size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
              </span>
            ) : null}
          </div>
        ) : null}
        {twoLine ? (
          // WHERE IT LIVES, on row 1's left: `folder › file` (the place, then
          // where in it), a chevron between when both stand.
          placeEl || where ? (
            <span className="task-head-crumb">
              {placeEl}
              {placeEl && where ? (
                <ChevronRight className="task-head-crumb-sep" size={11} aria-hidden="true" data-fit-skip="" />
              ) : null}
              {where ? <span className="task-head-where">{where}</span> : null}
            </span>
          ) : null
        ) : (
          marks
        )}
        {stackedNow ? (
          // STACKED: the title has row 2; the marks' own seat takes the
          // slack and the slots stand at the row's end (their `margin-left:
          // auto`). No spacer between them: it held nothing but cost the row
          // a gap, and the door's word folded a few px before it had to
          // (Akshil, 2026-11-16: "there is still space but Open word not
          // visible"). A head with no task keeps its place — and the spacer
          // that holds its two clusters apart — up here.
          // (Two lines with no task: the crumb already holds the place.)
          who || twoLine ? null : (
            <>
              {placeEl}
              <span className="task-head-idle" aria-hidden />
            </>
          )
        ) : (
          <>
            {identity ?? (who ? null : <span className="task-head-idle" aria-hidden />)}
            {trail}
            {who ? null : placeEl}
          </>
        )}
        <div className="task-head-marks">
          {chips || chipsRef ? (
            // `c-tokens`: the chat's worktree pill and PR chip paint from the
            // chat's `--c-*` palette, and this seat is outside `.chat-root`.
            <span className="task-head-chips c-tokens">
              {chipsRef ? <span className="task-head-chips-host" ref={chipsRef} /> : null}
              {chips}
            </span>
          ) : null}
          {seats ? (
            <span className="task-head-seats" ref={seatsRef}>
              {seats}
            </span>
          ) : null}
          {where && !twoLine ? <span className="task-head-where">{where}</span> : null}
          {splitRows ? (
            actsBox
          ) : clustered ? (
            factsCluster
          ) : (
            <>
              {countSlot}
              {timeSlot}
            </>
          )}
          {kebabInActs ? null : kebabEl}
        </div>
      </div>
      {stackedNow && (identity || line || status || statusRef || rowFacts) ? (
        <div className="task-head-titlerow">
          {twoLine ? marks : null}
          {identity}
          {trail}
          {rowFacts}
        </div>
      ) : null}
    </div>
  );
}

export default TaskHead;
