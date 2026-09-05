// The Tasks page's fourth view: CARDS — every task's conversation, side by
// side, the running ones streaming (Akshil, 2026-09-03: "like a eagle eye view
// of all chats streaming in at the same time"; 2026-09-05: "all tasks here
// except archived").
//
// It is the one view on this page that does not draw ROWS ABOUT tasks. The List,
// the Board and the Calendar all answer "what is there and where does it sit";
// this one answers "what is happening", and the only honest way to show that is
// to show the thing itself. So each card frames the real chat template over the
// real session — the same document the explorer's Claude sidebar frames — and
// the streaming comes for free: that template already polls its own run every
// 400ms, so this component has no feed, no socket and no cadence of its own. It
// lays out iframes and gets out of the way.
//
// Which makes the whole file's job the three things a grid of live documents
// gets wrong:
//
//   * PARAM ISOLATION. The chat template reads `session_id` through the runtime,
//     which climbs to the topmost same-origin ancestor — this page — unless an
//     ancestor says stop. Twelve cards climbing to `/tasks` would be twelve
//     chats sharing one session id. The boundary effect below is the stop sign.
//   * IDENTITY ACROSS POLLS. The page re-renders every 20 seconds with a fresh
//     array. A card keyed by anything but the task would remount its iframe on
//     each poll, which is a reload of a live conversation every 20 seconds.
//   * A BUDGET. Live documents are not free, so the wall is drawn a page of
//     nine at a time (tasks-lib.CARD_PAGE) and grows only when asked.
//
// Everything about WHICH tasks and in WHAT ORDER is tasks-lib.cardsForTasks —
// pure, tested, and out of here, because a wall of iframes is the last place
// anybody can test a sort.
import { useEffect, useMemo, useRef, useState } from "react";
import { archiveTask, statPath, unarchiveTask } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import { cardFrameSrc, folderHref, peekFrameSrc } from "./schedule-lib";
import { StatusIcon } from "./ScheduleTaskViews";
import {
  CARD_PAGE,
  cardKey,
  cardsForTasks,
  filingIntent,
  firstLine,
  opensElsewhere,
  taskColumn,
  taskHref,
  taskWhen,
  tildePath,
} from "./tasks-lib";
import { useMarginWheel } from "./useMarginWheel";

/** What the page says when there is nothing to draw — the Board's own words,
 *  whatever the reason (no tasks, or a filter narrowed them away), because the
 *  view's claim is about the set it was handed and it cannot tell those apart. */
export const CARDS_EMPTY = "Nothing to show here.";

/** The claude template's path for one folder, cached for this mounting.
 *
 *  It is a per-FOLDER answer and not a constant: the mode registry lets a user
 *  put their own template in front of `claude` for a path (§16), and a view that
 *  hardcoded the built-in would quietly ignore that override on exactly the
 *  surface where it is most visible. Resolved through the same call the canvas
 *  workspace uses for the same reason (/api/fs/stat → `templates`).
 *
 *  One request per DISTINCT folder, and most walls are one or two folders' worth
 *  of work, so this is a call or two rather than one per card. */
function useChatTemplates(dirs: string[]): Record<string, string> {
  const [paths, setPaths] = useState<Record<string, string>>({});
  // Folders already asked about — including the ones that ANSWERED with no
  // claude mode at all, which is why this is a set of asked and not a check of
  // `paths`: a folder with no chat template must be asked once, not once per
  // poll for as long as the card is up.
  const asked = useRef<Set<string>>(new Set());
  // `dirs` is a fresh array on every poll; the effect must fire on its
  // CONTENTS, or it re-runs 20 seconds apart forever (harmlessly, thanks to
  // `asked`, but it is a loop nobody should have to reason about).
  const key = dirs.join("\u0000");
  useEffect(() => {
    let cancelled = false;
    for (const dir of key.split("\u0000")) {
      if (!dir || asked.current.has(dir)) continue;
      asked.current.add(dir);
      void statPath(dir)
        .then((st) => {
          if (cancelled) return;
          const found = st.templates?.find((t) => t.mode === "claude")?.path;
          if (found) setPaths((m) => ({ ...m, [dir]: found }));
        })
        .catch(() => {
          // A folder that has gone away, or a stat that failed: the card falls
          // back to its "Starting…" pane rather than the page failing. There is
          // nothing to say here that the card does not already show.
        });
    }
    return () => {
      cancelled = true;
    };
  }, [key]);
  return paths;
}

export function TaskCards({
  tasks,
  home = "",
  onReload,
}: {
  /** Already filtered, in the SERVER's order — `cardsForTasks` drops the
   * archived ones and orders the rest, which is the one thing this view does to
   * the set it is handed and the one place it is decided. */
  tasks: Task[];
  home?: string;
  /** After the popup archives or unarchives: the card's lane changed, so the
   * page re-reads — the same call the Board's drops and the List's row make. */
  onReload?: () => void;
}) {
  // THE POPUP (Akshil, 2026-09-05): one task at a time, opened from a card's
  // head. Held as the Task itself and not a key — a task that leaves the set
  // (archived from inside the popup, or filtered away) keeps its popup until
  // the reader closes it, because the frame in it is a real session and
  // closing it under them would lose whatever they were typing.
  const [peek, setPeek] = useState<Task | null>(null);
  // HOW MANY PAGES THE READER HAS ASKED FOR. Nine cards to begin with, and each
  // press of the trailing card adds nine (Akshil, 2026-09-05). Never wound back
  // by a poll: the list refreshes every 20 seconds, and a wall that collapsed to
  // its first page each time would undo the reader's own gesture under them.
  const [pages, setPages] = useState(1);
  const { cards, hidden } = useMemo(() => cardsForTasks(tasks, pages * CARD_PAGE), [tasks, pages]);
  // The wheel works in the gutters either side of the column (Akshil,
  // 2026-09-05: "when I try to scroll, it does not scroll") — forwarded to the
  // wall, the List's own rule, rather than by widening the scroller, which is
  // what put the two views' scrollbars in different places.
  const wallRef = useRef<HTMLDivElement | null>(null);
  useMarginWheel(wallRef);
  // Distinct folders, in a stable order, so the template lookup below is keyed
  // on the SET and not on which card happened to sort first this poll.
  const dirs = useMemo(() => {
    const seen = new Set<string>();
    for (const t of cards) {
      const dir = t.target || t.project;
      if (dir) seen.add(dir);
    }
    return [...seen].sort();
  }, [cards]);
  const templates = useChatTemplates(dirs);

  // THE STOP SIGN. `fused.params` inside each card climbs window.parent until it
  // runs out of same-origin ancestors OR meets a param boundary, and stops BELOW
  // the boundary (static/runtime.js `findTarget`, D46/D72). Marking this window
  // is therefore what makes each card's own frame its own param target: it reads
  // the `session_id` it was given in its src, and a write from inside it lands on
  // its own URL instead of rewriting `/tasks` under everyone else.
  //
  // Set while THIS VIEW is mounted and removed on the way out, exactly as the two
  // layout shells do it (apps/explorer/Panel.tsx, Tabs.tsx) — the flag is a fact
  // about a window that is currently hosting param-owning frames, not a fact
  // about the app, and leaving it set would change how an unrelated iframe on
  // some other route resolves its params.
  useEffect(() => {
    window._fusedParamBoundary = true;
    return () => {
      delete window._fusedParamBoundary;
    };
  }, []);

  if (cards.length === 0) {
    // The Board's empty styling, deliberately — one page, one way of saying
    // there is nothing here.
    return <p className="schedule-tv-empty">{CARDS_EMPTY}</p>;
  }

  return (
    // The SCROLLER is this outer pane — the List's own shape (tasks.css
    // `.tasks-list`), so the bar sits where the List's does — and the grid is
    // one child of it, the "Show more" control another, full width beneath. Put
    // inside the grid the control was one cell of a row sized for a chat, with a
    // chat's worth of empty track under it (Akshil, 2026-09-05).
    <div className="task-cards-scroll" ref={wallRef}>
      <div className="task-cards">
      {cards.map((task) => (
        <TaskCard
          // KEYED BY THE TASK'S IDENTITY and nothing else. Not the index (a
          // card that finishes shifts every card after it, and React would
          // recycle each iframe into a different conversation), not the src (the
          // template path arrives one render late, and a changed key is a
          // reload). This is what makes a poll a re-render and not twelve
          // reloads.
          //
          // And not `task.key` either, which is what this was and which is a
          // key that CHANGES under one task: a scheduled run is listed as
          // `pending:<entry>` until its session reports, then relisted under the
          // session id (§5), so every card was being torn down and rebuilt about
          // two seconds after it appeared. `cardKey` carries across that
          // handover, and it says at length why the task NUMBER is the half that
          // does — and why the one case where even the number moves cannot reach
          // a card that has a session to frame.
          key={cardKey(task)}
          task={task}
          template={templates[task.target || task.project] ?? null}
          onPeek={setPeek}
        />
      ))}
      </div>
      {peek && (
        <TaskPeek
          task={peek}
          home={home}
          template={templates[peek.target || peek.project] ?? null}
          onClose={() => setPeek(null)}
          onReload={onReload}
        />
      )}
      {hidden > 0 && (
        <button
          type="button"
          className="task-cards-more"
          onClick={() => setPages((n) => n + 1)}
        >
          Show more
        </button>
      )}
    </div>
  );
}

function TaskCard({
  task,
  template,
  onPeek,
}: {
  task: Task;
  template: string | null;
  onPeek: (task: Task) => void;
}) {
  const when = taskWhen(task);
  const title = firstLine(task.title) || "(untitled)";
  // Both halves have to be there before anything can be framed: no session means
  // there is no conversation yet, and no template means the folder's stat has not
  // answered (or has no chat mode at all).
  const src = task.session_id && template
    ? cardFrameSrc(template, task.target || task.project, task.session_id)
    : null;

  return (
    <section className="task-card" aria-label={`${task.task_id} ${title}`}>
      {/* THE HEAD IS THE DOOR (Akshil, 2026-09-05: "when I click on the heading
          of the card ... it should open the preview"). The whole strip — ring,
          id, time, title — is one button that opens the task's popup; the body
          under it is the live chat and keeps its own clicks. A real button role
          with the keyboard's two keys, because a header that only a pointer
          can press is a door with no handle for everyone else. */}
      <header
        className="task-card-head"
        role="button"
        tabIndex={0}
        aria-label={`Preview ${task.task_id}`}
        onClick={() => onPeek(task)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onPeek(task);
          }
        }}
      >
        {/* TWO ROWS (Akshil, 2026-09-04): ring then id at the top-left — the
            List row's own order — with the time at the right, and the title
            alone on the second row so it gets the card's whole width at a size
            that can be read across a wall. No Open button (Akshil, 2026-09-05):
            the head itself opens the popup, and the popup carries the doors. */}
        <div className="task-card-head-row">
          {/* The ring is unconditional here, unlike the Board's. A card on this
              view sits under no lane header, so nothing else on it says what
              state the run is in — the same argument that keeps the ring on
              every List row and every Calendar chip. */}
          <StatusIcon status={taskColumn(task)} failed={task.failed} />
          <span className="tasks-id tasks-id--task">{task.task_id}</span>
          {/* The same relative unit every task row on this page prints, from the
              same function — so a card and its row agree about when this last
              moved (tasks-lib.taskWhen). */}
          <span className="task-card-when" title={when.title}>
            {when.text}
          </span>
        </div>
        {/* `data-hint`, not `title`: the List row shows its full title in the
            app's own hint the moment the pointer rests (hints.ts), and a native
            tooltip that arrives a second later read as no tooltip at all
            (Akshil, 2026-09-04). Same mechanism, same words, same delay. */}
        <span className="task-card-title" data-hint={task.title}>
          {title}
        </span>
      </header>
      <div className="task-card-body">
        {src ? (
          <iframe
            className="task-card-frame"
            src={src}
            title={`${task.task_id} ${title}`}
            // NO `sandbox`, like every other /render frame in this app. It would
            // have to carry `allow-same-origin allow-scripts` to work at all —
            // the template is a script that reads its own URL and talks to this
            // window through the runtime — and a sandbox holding both grants is
            // a sandbox that grants everything, with the side effect of being
            // the one frame in the app whose contract differs from the rest.
          />
        ) : (
          // No session to frame. For a scheduled task that is simply not due yet;
          // for anything else it is the window between "claimed and sent" and
          // "we know which chat that is" (schedule-lib, above `folderHref`) — a
          // real state, a few seconds to a few minutes long. Either way the card
          // says which rather than framing the wrong thing or an empty box.
          <p className="task-card-starting">
            {taskColumn(task) === "upcoming" ? "Not started yet" : "Starting…"}
          </p>
        )}
      </div>
    </section>
  );
}

/** The popup a card's head opens: the same task's chat at FULL size — a tall
 *  column, most of the window — with its composer, so a reader can answer a
 *  question or send the next message without leaving the wall (Akshil,
 *  2026-09-05), and the two doors the List row already has: the folder with the
 *  Claude pane, and Archive. Not a third one back to the List — "we are already
 *  in tasks" (Akshil, 2026-09-05).
 *
 *  Built on the app's one modal chassis (platform/ui/modal/Modal) so Esc, the
 *  backdrop, the ✕, the focus trap and the exit animation are the ones every
 *  other dialog has. `plainBody`, because the body is a frame and not a form. */
function TaskPeek({
  task,
  home,
  template,
  onClose,
  onReload,
}: {
  task: Task;
  home: string;
  template: string | null;
  onClose: () => void;
  onReload?: () => void;
}) {
  const title = firstLine(task.title) || "(untitled)";
  const src = task.session_id && template
    ? peekFrameSrc(template, task.target || task.project, task.session_id)
    : null;
  // The List row's own fallback: a run with no session yet is still reachable
  // through its folder (schedule-lib, above `folderHref`).
  const explorer = taskHref(task) ?? folderHref(task);
  const filing = filingIntent(task);
  const [acting, setActing] = useState(false);
  const [note, setNote] = useState("");

  // ESCAPE FROM INSIDE THE FRAME. The chassis closes on Esc with a listener on
  // THIS document, and the frame is the dialog's first focusable, so the focus
  // trap puts the caret in the chat — where the reader wants it — and every key
  // from then on fires in the frame's document, which the chassis cannot hear.
  // Measured: Esc did nothing while the composer had focus. Same origin, so the
  // frame's document takes a listener of its own; a key the template already
  // stops (its own popovers close on Esc and stopPropagation) never reaches
  // it, which is the right precedence — Esc closes the innermost thing open.
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    let doc: Document | null = null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const attach = () => {
      try {
        doc = frame.contentDocument;
        doc?.addEventListener("keydown", onKey);
      } catch {
        // A frame that is not ours (it never is — /render is same-origin — but
        // a listener is not worth a thrown error).
      }
    };
    frame.addEventListener("load", attach);
    attach();
    return () => {
      frame.removeEventListener("load", attach);
      doc?.removeEventListener("keydown", onKey);
    };
  }, [src, onClose]);

  const refile = async () => {
    if (!filing) return;
    setActing(true);
    setNote("");
    try {
      if (filing.kind === "archive") await archiveTask(task.key);
      else await unarchiveTask(task.key);
      // The card's lane changed (or it left the wall), so the page re-reads —
      // and the popup goes with it: a filed task is a closed matter.
      onReload?.();
      onClose();
    } catch (e) {
      // The server's own sentence, in the List row's quiet voice: nothing was
      // destroyed either way, which is the whole point of archiving.
      setNote((e as Error).message);
      setActing(false);
    }
  };

  return (
    <Modal
      title={
        <span className="task-peek-title">
          <StatusIcon status={taskColumn(task)} failed={task.failed} />
          <span className="tasks-id tasks-id--task">{task.task_id}</span>
          {/* Shrink-to-fit, so the hint rides the WORDS and not the empty run
              of head to their right (Akshil, 2026-09-05). */}
          <span className="task-peek-name" data-hint={task.title}>
            {title}
          </span>
        </span>
      }
      onClose={onClose}
      width="54vw"
      dialogClassName="task-peek"
      plainBody
      // THE DOORS, IN THE HEAD beside the ✕ (Akshil, 2026-09-05: "move them on
      // top where we have the close button"), each an icon WITH its word — an
      // icon alone was not clear — in the app's own small secondary button, the
      // one the toolbar above this popup wears. Order: the folder, then Archive.
      headActions={
        <>
          {explorer && (
            <a
              // A real link with a real href, so ⌘-click and middle-click open
              // the folder in a tab — the rule every row on this page follows.
              className="btn btn-secondary modal-head-act"
              href={explorer}
              title={tildePath(task.target || task.project, home)}
              onClick={(e) => {
                if (opensElsewhere(e)) return;
                e.preventDefault();
                onClose();
                navigateUrl(explorer);
              }}
            >
              {ICON_FOLDER}
              Open in Explorer
            </a>
          )}
          {filing && (
            <button
              type="button"
              className="btn btn-secondary modal-head-act"
              disabled={acting}
              title={filing.title}
              onClick={refile}
            >
              {filing.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
              {filing.label}
            </button>
          )}
        </>
      }
      // The footer exists only while there is a sentence for it: a refused
      // archive, in the List row's quiet voice.
      footer={
        note ? (
          <span className="task-peek-note" role="status">
            {note}
          </span>
        ) : undefined
      }
    >
      {src ? (
        <iframe
          ref={frameRef}
          className="task-peek-frame"
          src={src}
          title={`${task.task_id} ${title}`}
          // No `sandbox`, for the card frame's reason (TaskCard, above).
        />
      ) : (
        <p className="task-card-starting">
          {taskColumn(task) === "upcoming" ? "Not started yet" : "Starting…"}
        </p>
      )}
    </Modal>
  );
}

// The head's icons, in MenuIcons' own stroke (platform/ui/MenuIcons: 16px,
// 24-grid, 1.5 stroke, round joins) so they sit in the app's buttons at the
// weight its menus draw. Inline rather than added to that record because they
// are this popup's and nothing else's — a folder, an archive box.
const ICON_PROPS = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** Open in Explorer — the folder (MenuIcons.folder's outline). */
const ICON_FOLDER = (
  <svg {...ICON_PROPS}>
    <path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l1.8 2h8a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z" />
  </svg>
);

/** Archive — the box with its lid. */
const ICON_ARCHIVE = (
  <svg {...ICON_PROPS}>
    <path d="M3.5 5.5h17v3.5h-17z" />
    <path d="M5 9v9.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V9" />
    <path d="M10 13h4" />
  </svg>
);

/** Unarchive — the same box, the lid open and an arrow out of it. */
const ICON_UNARCHIVE = (
  <svg {...ICON_PROPS}>
    <path d="M5 9v9.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V9" />
    <path d="M3.5 9h17" />
    <path d="M12 16v-6M9.5 12.5 12 10l2.5 2.5" />
  </svg>
);
