// The Tasks page's fourth view: CARDS — every running conversation, side by
// side, streaming (Akshil, 2026-09-03: "like a eagle eye view of all chats
// streaming in at the same time").
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
//   * A BUDGET. Live documents are not free (tasks-lib.CARD_CAP).
//
// Everything about WHICH tasks and in WHAT ORDER is tasks-lib.cardsForTasks —
// pure, tested, and out of here, because a wall of iframes is the last place
// anybody can test a sort.
import { useEffect, useMemo, useRef, useState } from "react";
import { statPath } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { cardFrameSrc, folderHref } from "./schedule-lib";
import { StatusIcon } from "./ScheduleTaskViews";
import {
  cardKey,
  cardsForTasks,
  firstLine,
  opensElsewhere,
  taskColumn,
  taskHref,
  taskWhen,
  tildePath,
} from "./tasks-lib";

/** What the page says when nothing is running. The same words whatever the
 *  reason — nothing started, or a filter narrowed it away — because the view's
 *  claim is about the set it was handed, and it cannot tell those apart. */
export const CARDS_EMPTY = "Nothing running right now.";

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
  onShowRunning,
}: {
  /** Already filtered, in the SERVER's order — `cardsForTasks` picks the running
   * ones and orders them, which is the one thing this view does to the set it is
   * handed and the one place it is decided. */
  tasks: Task[];
  home?: string;
  /** "+N more running": take the reader to the List, narrowed to what this view
   * was showing. A callback and not an href because the filter lives in page
   * state rather than in the URL — and a link that PROMISED a filtered list and
   * delivered an unfiltered one would be the worse half of that trade. */
  onShowRunning?: () => void;
}) {
  const { cards, hidden } = useMemo(() => cardsForTasks(tasks), [tasks]);
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
          home={home}
          template={templates[task.target || task.project] ?? null}
        />
      ))}
      {hidden > 0 && (
        <button type="button" className="task-card task-card--more" onClick={onShowRunning}>
          {`+${hidden} more running`}
        </button>
      )}
    </div>
  );
}

function TaskCard({
  task,
  home,
  template,
}: {
  task: Task;
  home: string;
  template: string | null;
}) {
  // Same fallback the List row's popover makes: a run that is in flight but has
  // not reported its session yet is still reachable through its FOLDER, and
  // hiding the only way in during exactly the minutes somebody needs it is the
  // wrong trade (schedule-lib, above `folderHref`).
  const href = taskHref(task) ?? folderHref(task);
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
      <header className="task-card-head">
        {/* TWO ROWS (Akshil, 2026-09-04): ring then id at the top-left — the
            List row's own order — with the time and Open at the right, and the
            title alone on the second row so it gets the card's whole width at a
            size that can be read across a wall. */}
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
          {href && (
            <a
              // The app's own small secondary button, not a bare accent link: a
              // word in the corner of a card read as a label, not as a thing to
              // press (Akshil, 2026-09-03). Still an <a> with a real href, so
              // ⌘-click and middle-click keep working.
              className="btn btn-secondary task-card-open"
              href={href}
              aria-label={`Open ${task.task_id}`}
              onClick={(e) => {
                // A modified press is left entirely alone — ⌘-click means "open
                // that in a tab", and this page has no business intercepting it.
                // The same rule every row on this page follows.
                if (opensElsewhere(e)) return;
                e.preventDefault();
                navigateUrl(href);
              }}
              title={tildePath(task.target || task.project, home)}
            >
              Open
            </a>
          )}
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
          // The window between "claimed and sent" and "we know which chat that
          // is" (schedule-lib, above `folderHref`) — a real state, a few seconds
          // to a few minutes long, and the card says so rather than framing the
          // wrong thing or showing an empty box.
          <p className="task-card-starting">Starting…</p>
        )}
      </div>
    </section>
  );
}
