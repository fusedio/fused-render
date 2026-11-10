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
// ONLY THE TITLE FLEXES. Everything else is a mark or a control, and a mark
// that ellipsises is a mark that lies (the peek head's rule, task-peek.css).
// When even a floored title will not fit, seats leave in a measured order —
// never a breakpoint (row-fit.ts `TASK_HEAD_DROPS`, `data-fit` below):
//
//   1. ↗ Open folds into the ⋮ — the host adds it there (`foldedOpen`);
//   2. the place; 3. the TASK-nnn number;
//   4. the title leaves the row for a line of its own (`stacked: "auto"`).
//
// The chips are on no rung — the worktree pill has no other home in a shared
// head, so they shrink instead. The lead, the chevrons, the ring and the ⋮
// are on no rung either: the way out of a container and the way to its
// actions are what a narrow one must keep.
//
// WHAT THE HOST OWNS. The ⋮ is the host's `<Kebab …/>` handed in whole, because
// what it does on Archive and Delete (advance to the next task, go back to the
// list, close the dialog) is the host's business; the head only seats it. The
// same for `chips` and `seats`. And because only this head knows how far its
// ladder has folded, a `kebab` may be a function of the fit — the host then
// builds its `extraItems` with `foldedOpen(fit)`.
import type { ReactNode, Ref } from "react";
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronUp, X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { queueCaption, type QueueFacts } from "@platform/lib/queue";
import { QueuedCaption, TaskPeekWho } from "./TaskPeekWho";
import { TASK_HEAD_DROPS, useStripFit } from "./row-fit";

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
  /** The node the chat portals its worktree menu into (ChatMount
   *  `headChipsHost`) — a seat at the head of the chips. */
  chipsRef?: (el: HTMLElement | null) => void;
  /** ↗ Open. Null where the head IS the page. */
  open?: TaskHeadOpen | null;
  /** The host's ⋮ (a `Kebab`), or a function of the fit for a host whose ⋮
   *  must carry the folded door. Null before a session exists. */
  kebab?: ReactNode | ((fit: number) => ReactNode);
  /** The title on a row of its own: `"auto"` (default) lets the ladder decide
   *  at its last rung; `true` pins it (the rail's favourite card); `false`
   *  never stacks. Pinned or never, the ladder stops at rung 3. */
  stacked?: boolean | "auto";
  /** Arm the fit ladder (the peek: only while the panel is up). */
  fitEnabled?: boolean;
  className?: string;
}

/** Has the ladder folded ↗ Open away? Then the host's ⋮ carries the door. */
export function foldedOpen(fit: number): boolean {
  return fit >= 1;
}

// Module constants, because `useStripFit` re-arms its observers whenever the
// ladder's identity changes.
const DROPS_AUTO: readonly string[] = TASK_HEAD_DROPS;
const DROPS_NO_STACK: readonly string[] = TASK_HEAD_DROPS.slice(0, TASK_HEAD_DROPS.length - 1);

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
}: TaskHeadProps) {
  const [fit, rowRef] = useStripFit(stacked === "auto" ? DROPS_AUTO : DROPS_NO_STACK, fitEnabled);
  // THE TITLE MOVES ROWS HERE, which remounts it: a rename half-typed when the
  // ladder crosses its last rung loses its draft (an accepted edge case).
  const stackedNow = stacked === true || (stacked === "auto" && fit >= TASK_HEAD_DROPS.length);
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
  const keys = !!nav?.keyHints;

  return (
    <header
      className={"task-head" + (stackedNow ? " is-stacked" : "") + (className ? ` ${className}` : "")}
      data-fit={fit}
    >
      <div className="task-head-row" ref={rowRef}>
        {preLead || leadNode || lead || nav ? (
          <div className="task-head-acts">
            {preLead}
            {leadNode ? (
              leadNode
            ) : lead ? (
              <button
                ref={lead.ref}
                type="button"
                className={`task-head-btn task-head-lead is-${lead.kind}`}
                aria-label={blockedWhy ? `${lead.label} — ${blockedWhy}` : lead.label}
                data-hint={blockedWhy || lead.hint || lead.label}
                disabled={lead.disabled}
                onClick={() => lead.onPress()}
              >
                {leadIcon}
              </button>
            ) : null}
            {nav ? (
              <>
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
              </>
            ) : null}
          </div>
        ) : null}
        {who ? (
          <TaskPeekWho
            task={who}
            running={line ? false : running}
            star={star}
            part={stackedNow ? "marks" : "all"}
            {...(stackedNow ? {} : { className: "task-head-title-seat" })}
            {...(!stackedNow && titleId ? { titleId } : {})}
          />
        ) : title && !stackedNow ? (
          <span
            className="task-head-title-static task-head-title-seat"
            {...headingAttrs}
            {...(typeof title === "string" ? { title } : {})}
          >
            {title}
          </span>
        ) : (
          <span className={"task-head-idle" + (pending ? " is-pending" : "")} aria-hidden />
        )}
        {line ? <QueuedCaption line={line} /> : null}
        {status ? (
          <span className="task-head-status" aria-live="polite">
            {status}
          </span>
        ) : null}
        {place ? <span className="task-head-place">{place}</span> : null}
        <div className="task-head-marks">
          {chips || chipsRef ? (
            // `c-tokens`: the chat's worktree pill and PR chip paint from the
            // chat's `--c-*` palette, and this seat is outside `.chat-root`.
            <span className="task-head-chips c-tokens">
              {chipsRef ? <span className="task-head-chips-host" ref={chipsRef} /> : null}
              {chips}
            </span>
          ) : null}
          {seats}
          {open ? (
            // A real link with a real href, so ⌘-click opens a tab, exactly
            // like a row's own door.
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
              <ArrowUpRight size={13} strokeWidth={1.6} aria-hidden="true" />
              {open.word ?? "Open"}
            </a>
          ) : null}
          {kebabNode ? <span className="task-head-kebab">{kebabNode}</span> : null}
        </div>
      </div>
      {stackedNow && (who || title) ? (
        <div className="task-head-titlerow">
          {who ? (
            <TaskPeekWho task={who} part="title" {...(titleId ? { titleId } : {})} />
          ) : (
            <span
              className="task-head-title-static"
              {...headingAttrs}
              {...(typeof title === "string" ? { title } : {})}
            >
              {title}
            </span>
          )}
        </div>
      ) : null}
    </header>
  );
}

export default TaskHead;
