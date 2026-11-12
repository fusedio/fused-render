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
// THE CANONICAL ORDER, left to right, on one 44px row (`--task-head-h`):
//
//   [lead] [↑][↓] │ ring ★ TASK-nnn title ✎ [queue] [status] [place]
//                 │                   [chips…] [seats…] [↗ Open] [⋮]
//
//   * LEAD — exactly one corner control, the same 28px icon button either way:
//     × closes the container the reader opened (panel, card, dialog); ← goes
//     back to the list INSIDE the same container. No word beside the arrow —
//     the hint carries it ("Back to tasks").
//   * ↑↓ — only where there is a list behind the head to walk.
//   * IDENTITY — always `TaskPeekWho star`: ring, agent mark, ★, number, title
//     with its pencil. Or a static title ("Tasks") for a head with no task.
//   * the queue caption and the paused word trail the title, where the Tasks
//     row prints them; the place after them.
//   * CHIPS (worktree, PR), host SEATS, the ↗ OPEN door, and the ⋮ at the end.
//
// NEVER MOVED, NEVER HIDDEN (Akshil, 2026-11-10): the lead, the place, ↗ Open
// beside the ⋮, and the ⋮ — the way out, where the task lives, and the doors.
// Only the title flexes; when even a floored title will not fit, the rest
// gives in a measured order — never a breakpoint (row-fit.ts
// `TASK_HEAD_DROPS`, `data-fit` below), cumulatively:
//
//   1. the chips; 2. the host's seats; 3. the ↑↓ chevrons;
//   4. the whole identity takes row 2 and the place moves up beside the lead
//      — row 1 is [lead] [place] … [↗ Open] [⋮] (`stacked: "auto"`);
//   5. the TASK-nnn number, on row 2.
//
// WHAT THE HOST OWNS. The ⋮ is the host's `<Kebab …/>` handed in whole, because
// what it does on Archive and Delete (advance to the next task, go back to the
// list, close the dialog) is the host's business; the head only seats it. The
// same for `chips` and `seats`.
import { useState, type ReactNode, type Ref } from "react";
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronUp, X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { queueCaption, type QueueFacts } from "@platform/lib/queue";
import { QueuedCaption, TaskPeekWho } from "./TaskPeekWho";
import { TaskFileSeat, type FiledKind } from "./TaskFileSeat";
import { taskColumn } from "./tasks-lib";
import { TASK_HEAD_DROPS, TASK_HEAD_STACK_LEVEL, taskHeadNeed, useStripFit } from "./row-fit";

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
  onOpen(href: string): void | boolean | Promise<boolean | void>;
}

/** A queued chat's facts — the row's own, so the identity can wear the queued
 *  row's number, title and dashed ring (Topbar's rule). */
export type TaskHeadQueue = (QueueFacts & Pick<Partial<Task>, "task_id" | "title">) | null;

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
  /** The favourite star in the identity (default on). Off where the
   *  favourites switch is off (the rail card's legacy rule). */
  star?: boolean;
  /** A host's own star control in the star's seat (TaskPeekWho `starNode`). */
  starNode?: ReactNode;
  /** Over the ring, inside its seat (TaskPeekWho `statusOverlay`). */
  statusOverlay?: ReactNode;
  /** Ring facts only the host knows (TaskPeekWho `ring`): unread, held
   *  draft, failed. */
  ring?: { unread?: boolean; draftHeld?: boolean; failed?: boolean };
  /** A muted line after the title, in the title's row (TaskPeekWho). */
  subtitle?: ReactNode;
  /** After a rename the server took (TaskPeekWho `onRenamed`). */
  onRenamed?: () => void;
  /** The node the host's seats sit in (`.task-head-seats`) — the chat
   *  measures it to fold its seat labels (`useFitStrip`). */
  seatsRef?: (el: HTMLElement | null) => void;
  /** THE RING FILES THE TASK (TaskFileSeat): on by default for a task with a
   *  session — hovering the STATUS ICON turns the ring into Archive /
   *  Unarchive (the icon itself, not the header: task-head.css says why). A host's own `statusOverlay` wins; `false` turns it
   *  off (a host whose archive lives elsewhere). */
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
  place?: ReactNode;
  chips?: ReactNode;
  seats?: ReactNode;
  /** The node the chat portals its usage-limit word into (ChatMount
   *  `headStatusHost`) — beside the identity, on no rung of the ladder:
   *  "paused · resumes 4:00 AM" is the one fact the ring cannot carry. */
  statusRef?: (el: HTMLElement | null) => void;
  /** The node the chat portals its worktree menu into (ChatMount
   *  `headChipsHost`) — a seat at the head of the chips. */
  chipsRef?: (el: HTMLElement | null) => void;
  /** ↗ Open. Null where the head IS the page. */
  open?: TaskHeadOpen | null;
  /** The host's ⋮ (a `Kebab`), or a function of the ladder's fit. Null
   *  before a session exists. */
  kebab?: ReactNode | ((fit: number) => ReactNode);
  /** The identity on a row of its own: `"auto"` (default) lets the ladder
   *  decide at rung 4; `true` pins that layout (rungs 1–3 and 5 still
   *  measured); `false` never stacks and caps the ladder at rung 3. */
  stacked?: boolean | "auto";
  /** Arm the fit ladder (the peek: only while the panel is up). */
  fitEnabled?: boolean;
  className?: string;
}

/** Has the ladder folded ↗ Open away? Never any more (Akshil, 2026-11-10):
 *  Open stands beside the ⋮ at every width, so no host's ⋮ carries it. Kept
 *  so a host that still asks gets the honest answer. */
export function foldedOpen(_fit: number): boolean {
  return false;
}

// Module constants, because `useStripFit` re-arms its observers whenever the
// ladder's identity changes.
const DROPS_ALL: readonly string[] = TASK_HEAD_DROPS;
/** `stacked: false`: chips, seats, chevrons — never the identity's rungs. */
const DROPS_NO_STACK: readonly string[] = TASK_HEAD_DROPS.slice(0, TASK_HEAD_STACK_LEVEL - 1);

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
}: TaskHeadProps) {
  // A SPENT PRESS HANDS THE RING BACK (the Tasks row's `.is-refiled`): up on
  // the filing press, down when the pointer leaves the head — no selector can
  // say "hovered, but the hover already spent its press".
  const [refiled, setRefiled] = useState(false);
  // Measured on the whole head — both rows — so rung 5 can answer row 2.
  const [fit, headRef] = useStripFit(stacked === false ? DROPS_NO_STACK : DROPS_ALL, fitEnabled, taskHeadNeed);
  // THE IDENTITY MOVES ROWS HERE, which remounts it: a rename half-typed when
  // the ladder crosses rung 4 loses its draft (an accepted edge case).
  const stackedNow = stacked === true || (stacked === "auto" && fit >= TASK_HEAD_STACK_LEVEL);
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
  /** What both identity halves share: the host's star, overlay, reply line
   *  and rename hook. */
  // THE SEAT FILES WHAT THE RING SHOWS. The page knows a turn is live before
  // the listing does (TaskPeekWho `running`): for those seconds the row still
  // says done, `filingIntent` would offer Archive with no confirm, and the
  // press would stop the turn that just started (CodeRabbit, #2230). The seat
  // reads the same effective status the ring draws — a card being answered
  // (`needs_attention`) keeps its confirmed Archive, a queued line its own.
  const liveTurn = !line && running && !!who && taskColumn(who) !== "needs_attention";
  const seatTask = who && liveTurn ? ({ ...who, status: "in_progress" } as Task) : who;
  const whoExtras = {
    ...(starNode ? { starNode } : {}),
    ...(ring ? { ring } : {}),
    ...(statusOverlay
      ? { statusOverlay }
      : fileSeat && seatTask?.session_id
        ? {
            statusOverlay: (
              <TaskFileSeat
                task={seatTask}
                onPress={() => setRefiled(true)}
                {...(onFiled ? { onFiled } : {})}
              />
            ),
          }
        : {}),
    ...(subtitle ? { subtitle } : {}),
    ...(onRenamed ? { onRenamed } : {}),
  };
  const keys = !!nav?.keyHints;

  // The identity block, wherever it stands: on row 1, or — stacked — alone on
  // row 2, the whole block (ring, ★, number, title, reply).
  const identity = who ? (
    <TaskPeekWho
      // KEYED ON THE TASK: a walk to another task (⌃⇧J/K, ↑↓) under a
      // standing head drops a half-typed rename rather than saving it there.
      key={who.key}
      task={who}
      running={line ? false : running}
      star={star}
      className="task-head-title-seat"
      {...(titleId ? { titleId } : {})}
      {...whoExtras}
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
  // The queue caption and the paused word trail the identity, on its row —
  // and the host's status seat (`statusRef`, the chat's portaled pause word)
  // with them: on NO rung, and onto row 2 with the identity when stacked.
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
  const placeEl = place ? <span className="task-head-place">{place}</span> : null;

  return (
    // A GROUP, not a <header>: a head inside a panel, a card or a dialog is
    // not the page's banner, and several of them stood on one page as several
    // banner landmarks.
    <div
      ref={headRef}
      className={
        "task-head" +
        (stackedNow ? " is-stacked" : "") +
        (refiled ? " is-refiled" : "") +
        (className ? ` ${className}` : "")
      }
      // The press is spent until the pointer leaves the STATUS ICON (the
      // seat's trigger, task-head.css) — leaving the head covers a pointer
      // that left the window through it.
      onMouseLeave={refiled ? () => setRefiled(false) : undefined}
      onMouseOver={
        refiled
          ? (e) => {
              const at = e.target as Element | null;
              if (!at?.closest?.(".task-side-peek-status")) setRefiled(false);
            }
          : undefined
      }
      role="group"
      aria-label={ariaLabel}
      data-fit={fit}
    >
      <div className="task-head-row">
        {preLead || leadNode || lead || nav ? (
          <div className="task-head-acts">
            {preLead}
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
        {stackedNow ? (
          // STACKED: the place moves up beside the lead, and the gap holds the
          // doors at the row's end.
          <>
            {placeEl}
            <span className="task-head-idle" aria-hidden />
          </>
        ) : (
          <>
            {identity ?? <span className="task-head-idle" aria-hidden />}
            {trail}
            {placeEl}
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
          {open ? (
            // A real link with a real href, so ⌘-click opens a tab, exactly
            // like a row's own door. Never folded: it stands beside the ⋮.
            <a
              className="task-head-open"
              href={open.href}
              aria-label={open.label}
              data-hint={open.label}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                e.preventDefault();
                void open.onOpen(open.href);
              }}
            >
              {open.arrow === false ? null : <ArrowUpRight size={13} strokeWidth={1.6} aria-hidden="true" />}
              {open.word ?? "Open"}
            </a>
          ) : null}
          {kebabNode ? <span className="task-head-kebab">{kebabNode}</span> : null}
        </div>
      </div>
      {stackedNow && (identity || line || status || statusRef) ? (
        <div className="task-head-titlerow">
          {identity}
          {trail}
        </div>
      ) : null}
    </div>
  );
}

export default TaskHead;
