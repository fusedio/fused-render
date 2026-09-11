// The Tasks page's fourth view: CARDS — every task's conversation, side by
// side, the running ones streaming (Akshil, 2026-09-03: "like a eagle eye view
// of all chats streaming in at the same time"; 2026-09-05: every status,
// "even archived ones").
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
//     six at a time (tasks-lib.CARD_PAGE) and grows only when asked.
//
// Everything about WHICH tasks and in WHAT ORDER is tasks-lib.cardsForTasks —
// pure, tested, and out of here, because a wall of iframes is the last place
// anybody can test a sort.
import { useEffect, useMemo, useRef, useState } from "react";
import { archiveTask, statPath, unarchiveTask } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { notify } from "@platform/lib/notifications";
import { ChatFramePlaceholder } from "@platform/ui/ChatFrame";
import { ChatMount, useNativeChatEnabled, useNativeChatFlag } from "@apps/claude";
import { Modal } from "@platform/ui/modal/Modal";
import { cardFrameSrc, folderHref, peekFrameSrc } from "./schedule-lib";
import {
  ICON_ARCHIVE,
  ICON_TRASH,
  ICON_UNARCHIVE,
  IdentityChip,
  StatusIcon,
} from "./ScheduleTaskViews";
import { EraseTaskModal } from "./EraseTaskModal";
import {
  CARD_PAGE,
  basename,
  cardKey,
  cardsForTasks,
  ERASE_BLOCKED_HINT,
  emptyPaneFailed,
  emptyPaneText,
  eraseBlocked,
  filingIntent,
  firstLine,
  opensElsewhere,
  spansProjects,
  taskColumn,
  taskHref,
  taskWhen,
  tildePath,
} from "./tasks-lib";
import { MISSING_FOLDER_TOAST, taskFolder, toastMissingFolder } from "./useMissingFolders";
import { useMarginWheel } from "./useMarginWheel";

/** What the page says when there is nothing to draw — the Board's own words,
 *  whatever the reason (no tasks, or a filter narrowed them away), because the
 *  view's claim is about the set it was handed and it cannot tell those apart. */
export const CARDS_EMPTY = "Nothing to show here.";

/** The claude template's path for one folder, cached for the LIFE OF THE PAGE.
 *
 *  It is a per-FOLDER answer and not a constant: the mode registry lets a user
 *  put their own template in front of `claude` for a path (§16), and a view that
 *  hardcoded the built-in would quietly ignore that override on exactly the
 *  surface where it is most visible. Resolved through the same call the canvas
 *  workspace uses for the same reason (/api/fs/stat → `templates`).
 *
 *  MODULE LEVEL, not a ref inside the hook, because the answer outlives the
 *  mounting that asked for it. This view remounts constantly — List → Cards and
 *  back, the popup opening, the app page's Tasks tab — and a cache scoped to one
 *  mounting meant every re-entry re-resolved every folder and drew a whole wall
 *  of "Starting…" for a few hundred milliseconds first. A stat's answer about
 *  which template serves a folder does not change under us often enough to be
 *  worth that; a reload re-reads it.
 *
 *  `null` in the map is a real answer — "asked, and this folder has no claude
 *  mode at all" — and must be kept, or a folder without a chat template is
 *  re-asked once per poll for as long as a card is up. `undefined` (absent) is
 *  therefore the ONLY "not known yet", which is what lets a card tell resolving
 *  apart from nothing-to-frame (see TaskCard). */
const chatTemplateCache = new Map<string, string | null>();
/** Folders being stat'd right now, so two mountings (the wall and its popup, or
 *  a remount landing mid-flight) share one request instead of racing two. */
const chatTemplateInFlight = new Map<string, Promise<void>>();

function resolveChatTemplate(dir: string): Promise<void> {
  const running = chatTemplateInFlight.get(dir);
  if (running) return running;
  const p = statPath(dir)
    .then((st) => {
      chatTemplateCache.set(dir, st.templates?.find((t) => t.mode === "claude")?.path ?? null);
    })
    .catch(() => {
      // A folder that has gone away, or a stat that failed. Recorded as "no
      // chat template here" rather than left unknown: unknown means a skeleton
      // forever, and there is nothing this card can frame either way. WHY it
      // failed is the page's question, answered once for every view by
      // Scheduled's useMissingFolders and handed down as `missing`.
      chatTemplateCache.set(dir, null);
    })
    .finally(() => {
      chatTemplateInFlight.delete(dir);
    });
  chatTemplateInFlight.set(dir, p);
  return p;
}

/** The template path per folder: a string, `null` for "no chat template here",
 *  or ABSENT while the folder is still being resolved.
 *
 *  Seeded from the cache SYNCHRONOUSLY on the first render, which is the whole
 *  point of the cache being module-level: a remount frames its cards on the
 *  render that mounts them, with no resolving beat in between. */
function useChatTemplates(dirs: string[]): Record<string, string | null> {
  const seed = () => {
    const known: Record<string, string | null> = {};
    for (const dir of dirs) {
      if (chatTemplateCache.has(dir)) known[dir] = chatTemplateCache.get(dir) ?? null;
    }
    return known;
  };
  const [paths, setPaths] = useState<Record<string, string | null>>(seed);
  // `dirs` is a fresh array on every poll; the effect must fire on its
  // CONTENTS, or it re-runs 20 seconds apart forever (harmlessly, thanks to the
  // cache, but it is a loop nobody should have to reason about).
  const key = dirs.join("\u0000");
  useEffect(() => {
    let cancelled = false;
    const publish = (dir: string) => {
      if (cancelled) return;
      setPaths((m) =>
        dir in m && m[dir] === (chatTemplateCache.get(dir) ?? null)
          ? m
          : { ...m, [dir]: chatTemplateCache.get(dir) ?? null },
      );
    };
    for (const dir of key.split("\u0000")) {
      if (!dir) continue;
      // Already answered — including by another mounting since this one's state
      // was seeded, which is why this publishes rather than skipping.
      if (chatTemplateCache.has(dir)) {
        publish(dir);
        continue;
      }
      void resolveChatTemplate(dir).then(() => publish(dir));
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
  onPickProject,
  pinnedProjects = [],
  missing,
  emptyLabel = CARDS_EMPTY,
}: {
  /** The page's sentence for an empty set (Scheduled `emptyLabel`) — the same
   * one the List, Board and Calendar print, so the four views agree. */
  emptyLabel?: string;
  /** Already filtered, in the SERVER's order — `cardsForTasks` orders it by
   * lane (every lane, Archive last), which is the one thing this view does to
   * the set it is handed and the one place it is decided. */
  tasks: Task[];
  home?: string;
  /** After the popup archives or unarchives: the card's lane changed, so the
   * page re-reads — the same call the Board's drops and the List's row make. */
  onReload?: () => void;
  /** The folder chip is the List row's FILTER TAG (Akshil, 2026-09-05): pressed,
   * it narrows the page to that project; pressed again, lets it go. The same
   * handler the List is handed, so the two chips cannot mean different things. */
  onPickProject?: (project: string) => void;
  /** Which projects the page is pinned to — the chip wears the ON state, and
   * survives the filter that makes every card agree (see `showProject`). */
  pinnedProjects?: string[];
  /** Folders the disk no longer has — the SAME set the List and the Board read
   * (Scheduled → useMissingFolders), so the three views can never disagree
   * about one folder. A card in one says "Folder no longer exists" and its
   * folder door goes disabled. */
  missing?: ReadonlySet<string>;
}) {
  // THE POPUP (Akshil, 2026-09-05): one task at a time, opened from a card's
  // head. Held as the Task the head was clicked with, then REFRESHED from every
  // poll below: a popup opened in the "Starting…" window — the usual moment,
  // right after creating a task — has no session id yet, and the one that
  // arrives two seconds later has to reach it or the popup never frames the
  // chat (Bugbot, #1009). The clicked Task stays as the fallback so a task that
  // leaves the set (archived from inside the popup, or filtered away) keeps its
  // popup until the reader closes it: the frame in it is a real session, and
  // closing it under them would lose whatever they were typing.
  const [peek, setPeek] = useState<Task | null>(null);
  const peekLive = useMemo(() => {
    if (!peek) return null;
    const id = cardKey(peek);
    const byKey = tasks.find((t) => cardKey(t) === id);
    if (byKey) return byKey;
    // THE HANDOVER (Bugbot, #1015). A popup opened on a "Starting…" card holds
    // a `pending:<entry>` row, and the row that replaces it two seconds later
    // is keyed by the session — a different cardKey now — so by key alone the
    // popup would keep the placeholder and never frame the chat. The task
    // NUMBER is what usually carries across that moment (ensure_ids' rekeys
    // pass), so a peek WITHOUT a session may follow its number to the row that
    // has one. Only a session-less peek: a peek with a session has a key that
    // never changes, and following the number from THERE is exactly how a twin
    // — two sessions under one number — got opened in the first place (cardKey).
    if (!peek.session_id && peek.task_id) {
      const settled = tasks.find(
        (t) => t.session_id && t.project === peek.project && t.task_id === peek.task_id,
      );
      if (settled) return settled;
    }
    return peek;
  }, [peek, tasks]);
  // ...and the settled row is WRITTEN BACK as the peek (Bugbot, #1015, round
  // 2): left as the pending snapshot, every later poll would re-resolve by the
  // number this view stopped trusting, and a failed poll (`tasks` empty) or a
  // filter dropping the row would fall back to the placeholder and tear down
  // the framed chat. Adopted once, the popup matches by key from then on and
  // keeps the settled snapshot as its fallback like any other peek.
  useEffect(() => {
    if (peek && peekLive && peekLive !== peek && !peek.session_id && peekLive.session_id) {
      setPeek(peekLive);
    }
  }, [peek, peekLive]);
  // HOW MANY PAGES THE READER HAS ASKED FOR. Six cards to begin with, and each
  // press of the trailing strip adds six (Akshil, 2026-09-05). Never wound back
  // by a poll: the list refreshes every 20 seconds, and a wall that collapsed to
  // its first page each time would undo the reader's own gesture under them.
  const [pages, setPages] = useState(1);
  const { cards, hidden } = useMemo(() => cardsForTasks(tasks, pages * CARD_PAGE), [tasks, pages]);
  // The List's rule for whether the chip is worth its pixels: only when the
  // cards span more than one folder — or the page is pinned to one, in which
  // case the chip is the thing that says so and the way to let it go.
  const showProject = useMemo(
    () => spansProjects(cards) || pinnedProjects.length > 0,
    [cards, pinnedProjects],
  );
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
  //
  // FLAG ON there are no param-owning frames here at all: every card's chat is
  // native and reads a MEMORY store of its own (ChatMount), so the flag would be
  // a claim about this window that is not true.
  //
  // TRI-STATE, and only a real `false` sets it. Read as a boolean, `null` ("the
  // prefs read has not landed") set the flag and deleted it one paint later —
  // a claim about the window that was never true. Nothing reads it at boot
  // today, so the cost was only honesty; the fix is to wait for the answer.
  // The legacy path is byte-identical: a `false` sets it while mounted and
  // removes it on the way out, exactly as before.
  const nativeChatState = useNativeChatFlag();
  useEffect(() => {
    if (nativeChatState !== false) return;
    window._fusedParamBoundary = true;
    return () => {
      delete window._fusedParamBoundary;
    };
  }, [nativeChatState]);

  // The popup outlives the wall it was opened from: a failed poll empties
  // `tasks`, and a filter can drop the last card, while someone is typing into
  // the frame — so it is rendered beside whichever of the two the wall draws,
  // never inside the branch that has cards (Bugbot, #1009).
  const popup = peekLive && (
    <TaskPeek
      task={peekLive}
      home={home}
      // NOT `?? null`: absent means the folder is still being resolved and
      // the body shows a skeleton, where null means there is nothing to frame.
      template={templates[peekLive.target || peekLive.project]}
      folderMissing={missing?.has(taskFolder(peekLive)) ?? false}
      onClose={() => setPeek(null)}
      onReload={onReload}
    />
  );

  if (cards.length === 0) {
    // The Board's empty styling, deliberately — one page, one way of saying
    // there is nothing here.
    return (
      <>
        <p className="schedule-tv-empty">{emptyLabel}</p>
        {popup}
      </>
    );
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
          // `cardKey` — the row key, since 2026-09-06 — rather than the task
          // NUMBER it was for three days: two sessions can share a number (the
          // rekey at the pending → session handover is refused when the session
          // already holds one), and a wall keyed on the number drew one of the
          // two and opened the other. cardKey says why the handover remount
          // this gives back only ever rebuilds a "Starting…" placeholder.
          key={cardKey(task)}
          task={task}
          home={home}
          // Absent while the folder resolves, null when it has no chat
          // template — the card draws a different body for each.
          template={templates[task.target || task.project]}
          folderMissing={missing?.has(taskFolder(task)) ?? false}
          onPeek={setPeek}
          onReload={onReload}
          project={
            showProject
              ? { pinned: pinnedProjects.includes(task.project), onPick: onPickProject }
              : null
          }
        />
      ))}
      </div>
      {popup}
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
  home,
  template,
  folderMissing,
  onPeek,
  onReload,
  project,
}: {
  task: Task;
  home: string;
  /** The folder is gone from the disk (Scheduled → useMissingFolders). */
  folderMissing: boolean;
  /** The folder's chat template: a path, `null` when the folder has none, and
   * `undefined` while the stat behind it is still in flight — three states,
   * because the body says something different for each (below). */
  template: string | null | undefined;
  onPeek: (task: Task) => void;
  /** After a door archives or unarchives: the card's lane changed, so the page
   * re-reads (the popup's own rule, TaskPeek). */
  onReload?: () => void;
  /** Draw the folder chip, and how: null hides it (one folder, nothing to tell
   * apart); otherwise whether the page is pinned to it and the tag's handler. */
  project: { pinned: boolean; onPick?: (project: string) => void } | null;
}) {
  const when = taskWhen(task);
  const title = firstLine(task.title) || "(untitled)";
  // Both halves have to be there before anything can be framed: no session means
  // there is no conversation yet, and no template means the folder's stat has
  // not answered (or has no chat mode at all).
  // A GONE folder frames nothing, whatever the template cache still remembers
  // (Bugbot, #1023): the cache is module-level and outlives the page, so a
  // folder deleted between two visits would still have a path here and the
  // card would frame the Explorer's stat error.
  const src = task.session_id && template && !folderMissing
    ? cardFrameSrc(template, task.target || task.project, task.session_id)
    : null;
  // THE DOORS ON THE HEAD (Akshil, 2026-09-06): the popup's two, Archive (or
  // Unarchive) and the folder, shown on hover over the title's right end so a
  // reader can file a card or step into its folder without opening the popup
  // first. Same intent, same href, same calls as TaskPeek's head — one set of
  // doors drawn in two places, not two sets.
  // THE FOURTH STATE of the pane (after frame, resolving, no-session text): the
  // folder is gone — the page's stat 404'd — so nothing will ever be
  // framed and the pane says so in the error colour every other view gives the
  // same fact (List row, Board card: "Folder missing").
  const gone = folderMissing;
  // The folder door goes DISABLED on a folder that is gone (Akshil, 2026-09-06:
  // "show disabled explorer button with same message"): its href would be the
  // Explorer at that path, which answers with the raw stat error this whole
  // change exists to stop (Bugbot, #1023), so the door stays where the eye
  // expects it, greyed, and says why on hover and on press. Archive stays live —
  // it is the way out.
  const explorer = gone ? null : (taskHref(task) ?? folderHref(task));
  const filing = filingIntent(task);
  const [acting, setActing] = useState(false);
  const [note, setNote] = useState("");
  // THE DELETE DOOR, on EVERY card (design.md §2's open question, answered:
  // "all cards", matching the chat's kebab rather than the List's row — the
  // List's trash is on a folder-missing row because that row has nothing else
  // left, which is a fact about that row and not about the verb).
  //
  // Its guard is the server's own (tasks-lib.eraseBlocked): a run in flight —
  // in_progress OR a needs_attention turn parked on a permission card — cannot
  // have its transcript pulled out from under it, so the door greys and its
  // hint says the only thing that would help. Disabled means the dialog never
  // opens, so nobody reads the refusal for the first time inside a confirmation.
  const [erasing, setErasing] = useState(false);
  const blocked = eraseBlocked(task);
  const refile = async () => {
    if (!filing || acting) return;
    setActing(true);
    setNote("");
    try {
      if (filing.kind === "archive") await archiveTask(task.key);
      else await unarchiveTask(task.key);
      onReload?.();
    } catch (e) {
      // No footer on a card: the server's sentence goes up as a toast — the
      // pointer may have left the door by the time the refusal lands — and
      // stays on the door's hint for the next attempt.
      const said = (e as Error).message;
      setNote(said);
      notify({ title: said, tone: "error" });
    } finally {
      setActing(false);
    }
  };
  // THE THIRD STATE, and the reason `template` is not just a path-or-null: the
  // task HAS a session, so there is a conversation to show, and the only thing
  // missing is which template shows it — a fact this card is a few hundred
  // milliseconds from having. That is a chat that has not arrived yet, not a
  // run that has not started, so it wears the frame's own skeleton and says
  // nothing. "Starting…" here was the wall's popcorn (design.md).
  const resolving = !src && !folderMissing && !!task.session_id && template === undefined;

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
          // Only a key pressed ON THE HEAD. The folder chip inside it is a real
          // button of its own; its Enter and Space bubble here, and answering
          // them would cancel the chip's press and open the popup instead of
          // filtering (Bugbot, #1011).
          if (e.target !== e.currentTarget) return;
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
          {/* The folder, as the List row prints it — the same chip, the same
              basename, the full path on hover — at the right, before the time
              (Akshil, 2026-09-05: "same here, top right corner, left side of
              time"). The List's order too: folder first, time last, because
              the last thing before the edge is the one a reader lands on and
              the time is what changes. And the List's TAG, not a label
              (Akshil, 2026-09-05: "when we click on them they filter?"): the
              chip stops its own press (IdentityChip's shield), so pressing the
              folder filters the page and does not also open the popup. */}
          {project && (
            <IdentityChip
              name={basename(task.project)}
              title={tildePath(task.project, home)}
              onPick={project.onPick && (() => project.onPick?.(task.project))}
              active={project.pinned}
            />
          )}
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
        {/* Inside the head (so hovering them keeps the head hovered) but not OF
            it: a press here stops before the head's onClick, so a door never
            also opens the popup. Keys are already the head's concern only when
            pressed on the head itself (onKeyDown above). */}
        {/* `data-hint=""` is the OPT-OUT (hints.ts): the strip sits over the
            title, and the hint panel resolves by piercing the stack under the
            pointer, so without it a door answered with the task's name (Akshil,
            2026-09-06). Each door carries its own hint instead of a native
            `title` — the app's panel shows on pointerover, a title after the
            browser's second, which read as no caption at all.

            Drawn only when it has a door to hold (a card with nothing to file,
            a folder that opens, and a folder that is not gone has none). */}
        {(filing || explorer || gone) && (
        <span className="task-card-doors" data-hint="" onClick={(e) => e.stopPropagation()}>
          {/* Delete for good — ONLY on a card whose folder is gone (Akshil,
              2026-09-07: "should only show up if it has a folder missing
              error"): a task that can still be opened is archived, not
              deleted, and a trash on every card read as an invitation. It
              sits LEFT of Archive (Akshil, 2026-09-07), first in the strip,
              on every surface that has the strip: card, Board, popup. */}
          {gone && (
<button
            type="button"
            className="task-card-door task-card-door--danger"
            disabled={blocked || acting}
            data-hint={blocked ? ERASE_BLOCKED_HINT : "Delete task forever"}
            aria-label={`Delete ${task.task_id} forever`}
            onClick={(e) => {
              e.stopPropagation();
              if (blocked || acting) return;
              setErasing(true);
            }}
          >
            {ICON_TRASH}
          </button>
          )}
          {filing && (
            <button
              type="button"
              className="task-card-door"
              disabled={acting}
              data-hint={note || filing.label}
              aria-label={filing.label}
              onClick={refile}
            >
              {filing.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
            </button>
          )}
          {explorer && (
            <a
              // A real link with a real href, so ⌘-click and middle-click open
              // the folder in a tab — the rule every row on this page follows.
              className="task-card-door"
              href={explorer}
              data-hint={`Open in Explorer — ${tildePath(task.target || task.project, home)}`}
              aria-label="Open in Explorer"
              onClick={(e) => {
                if (opensElsewhere(e)) return;
                e.preventDefault();
                navigateUrl(explorer);
              }}
            >
              {ICON_FOLDER}
            </a>
          )}
          {gone && (
            <button
              type="button"
              className="task-card-door is-disabled"
              aria-disabled="true"
              data-hint={MISSING_FOLDER_TOAST}
              aria-label="Open in Explorer — folder deleted"
              onClick={toastMissingFolder}
            >
              {ICON_FOLDER}
            </button>
          )}
        </span>
        )}
      </header>
      <div className="task-card-body">
        {src ? (
          // The frame and its cover, one component (platform/ui/ChatFrame): the
          // iframe stays invisible until the chat inside it says its transcript
          // is painted, with the skeleton over it until then. The card's own
          // class rides the iframe, so the scaled fit below is untouched.
          //
          // FLAG ON, the native chat renders in place of that frame and the
          // class is deliberately NOT stamped on it: `.task-card-frame` is the
          // 133.33%/scale(0.75) fit, which is exactly what the native compact
          // variant replaces with a type scale (apps/claude/styles/chat.css).
          // `session_id` goes into a MEMORY param store per card, which is what
          // `_fusedParamBoundary` bought the frame (00 §1e).
          <ChatMount
            legacySrc={src}
            className="task-card-frame"
            title={`${task.task_id} ${title}`}
            file={task.target || task.project}
            sessionId={task.session_id}
            chatOnly
            compact
            paramsSource="memory"
          />
        ) : resolving ? (
          <ChatFramePlaceholder />
        ) : (
          // No session to frame. For a scheduled task that is simply not due yet;
          // for anything else it is the window between "claimed and sent" and
          // "we know which chat that is" (schedule-lib, above `folderHref`) — a
          // real state, a few seconds to a few minutes long. Either way the card
          // says which rather than framing the wrong thing or an empty box.
          // ...and the error colour is asked for, not re-derived: a settled
          // task with no session is a broken promise of the same kind as a
          // missing folder (`emptyPaneFailed`, FIX-A).
          <p
            className={
              "task-card-starting" + (emptyPaneFailed(task, gone) ? " is-missing" : "")
            }
          >
            {emptyPaneText(task, gone)}
          </p>
        )}
      </div>
      {erasing && (
        <EraseTaskModal
          task={task}
          onClose={() => setErasing(false)}
          onDone={() => {
            setErasing(false);
            // The card is about to leave the wall, so the receipt goes to the
            // page's toast rather than onto the card's own note line.
            notify({ title: `Deleted ${task.task_id}`, tone: "info", tier: "trail" });
            onReload?.();
          }}
        />
      )}
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
  folderMissing,
  onClose,
  onReload,
}: {
  task: Task;
  home: string;
  /** As TaskCard's: path, `null` for none, `undefined` while resolving. */
  template: string | null | undefined;
  /** As TaskCard's. */
  folderMissing: boolean;
  onClose: () => void;
  onReload?: () => void;
}) {
  const title = firstLine(task.title) || "(untitled)";
  const src = task.session_id && template && !folderMissing
    ? peekFrameSrc(template, task.target || task.project, task.session_id)
    : null;
  // The card's third and fourth states, for the card's reasons (TaskCard, above).
  const resolving = !src && !folderMissing && !!task.session_id && template === undefined;
  const gone = folderMissing;
  // The List row's own fallback: a run with no session yet is still reachable
  // through its folder (schedule-lib, above `folderHref`) — unless the folder
  // is gone, when the door goes disabled and says why (the card's rule).
  const explorer = gone ? null : (taskHref(task) ?? folderHref(task));
  const filing = filingIntent(task);
  // Same guard as the card's door: a live run cannot be erased (409), so the
  // popup's Delete greys out and says why instead of opening a doomed confirm.
  const blocked = eraseBlocked(task);
  const [acting, setActing] = useState(false);
  const [note, setNote] = useState("");
  const [erasing, setErasing] = useState(false);

  // ESCAPE FROM INSIDE THE FRAME. The chassis closes on Esc with a listener on
  // THIS document, and the frame is the dialog's first focusable, so the focus
  // trap puts the caret in the chat — where the reader wants it — and every key
  // from then on fires in the frame's document, which the chassis cannot hear.
  // Measured: Esc did nothing while the composer had focus. Same origin, so the
  // frame's document takes a listener of its own; a key the template already
  // stops (its own popovers close on Esc and stopPropagation) never reaches
  // it, which is the right precedence — Esc closes the innermost thing open.
  // FLAG ON there is no frame and no second document: the chat is in THIS one,
  // its root hands Escape up through `onEscape`, and the thing worth focusing is
  // the composer's textarea rather than a box around it. Both refs are declared
  // either way; exactly one of them is the live one.
  const native = useNativeChatEnabled();
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  // WHEN THE COMPOSER EXISTS. `boxRef` is filled by an effect inside the chat,
  // behind a `lazy` boundary — so at the moment the chassis computes
  // `initialFocus` it is still null and the caret fell to the ✕, which is
  // exactly what this popup exists not to do. The chat's own ready signal is
  // the honest trigger: bumped once the transcript paints, it tells the Modal
  // to take the focus it could not take at mount. Legacy is unaffected —
  // `frameRef` is a render-time ref and was live at mount all along, so no
  // signal is passed on that path (and no `onReady` either, which would
  // otherwise land on the iframe's `load`).
  const [chatReady, setChatReady] = useState(0);
  useEffect(() => {
    if (native) return;
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
  }, [native, src, onClose]);

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
    <>
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
      // While the confirm is up the popup is INERT to its own closers: the
      // confirm is a second Modal portaled after this one (so it paints above
      // and holds focus — the chassis leaves a nested [role="dialog"] alone),
      // and Escape reaches both document listeners; this one must not also
      // close the popup, or an Esc on the confirm ends the whole thing. The
      // popup stays MOUNTED, so the chat frame keeps its draft and its Escape
      // handler (review, PR #1049: unmounting it reloaded the frame).
      onClose={erasing ? () => {} : onClose}
      width="54vw"
      dialogClassName="task-peek"
      plainBody
      // The caret goes to the CHAT, not to the head's first button: the popup
      // exists so a reader can type, and a keyboard Enter after opening it must
      // not follow "Open in Explorer" instead (Bugbot, #1009). Null while the
      // frame is not there yet ("Starting…"), and the chassis then falls back
      // to its first focusable as every other dialog does.
      // The composer natively, the frame in the legacy path (see `native`).
      initialFocus={native ? boxRef : frameRef}
      // See `chatReady`: natively the ref fills after the chunk resolves, so the
      // chassis re-runs its initial focus when the chat says it is up.
      {...(native ? { focusSignal: chatReady } : {})}
      // THE DOORS, IN THE HEAD beside the ✕ (Akshil, 2026-09-05: "move them on
      // top where we have the close button"), each an icon WITH its word — an
      // icon alone was not clear — in the app's own small secondary button, the
      // one the toolbar above this popup wears. Order: Archive, then the folder
      // (Akshil, 2026-09-05: "switch archive and open explorer buttons").
      headActions={
        <>
          {/* Same door, same place as the card's strip: delete for good, only
              when the folder is gone, LEFT of Archive (Akshil, 2026-09-07). */}
          {gone && (
            <button
              type="button"
              className="btn btn-secondary modal-head-act modal-head-act--danger"
              disabled={acting || blocked}
              title={blocked ? ERASE_BLOCKED_HINT : "Delete task forever"}
              onClick={() => setErasing(true)}
            >
              {ICON_TRASH}
              Delete
            </button>
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
          {gone && (
            <button
              type="button"
              className="btn btn-secondary modal-head-act"
              aria-disabled="true"
              aria-label="Open in Explorer — folder deleted"
              data-hint={MISSING_FOLDER_TOAST}
              onClick={toastMissingFolder}
            >
              {ICON_FOLDER}
              Open in Explorer
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
        // The card's wrapper, at full size. `legacyFrameRef` still reaches the
        // iframe itself — the LEGACY Esc listener above and the chassis's
        // `initialFocus` both want the element, not the box around it.
        //
        // FLAG ON there is no frame to listen inside: `onEscape` is the same
        // close, handed up from the chat's own root, and `focusRef` is the
        // composer's textarea — which is a better `initialFocus` than the
        // iframe ever was, since it is where the reader actually wants the
        // caret (TaskCards' own note above `frameRef`).
        <ChatMount
          legacySrc={src}
          legacyFrameRef={frameRef}
          className="task-peek-frame"
          title={`${task.task_id} ${title}`}
          file={task.target || task.project}
          sessionId={task.session_id}
          chatOnly
          peek
          paramsSource="memory"
          onEscape={onClose}
          focusRef={boxRef}
          {...(native ? { onReady: () => setChatReady((n) => n + 1) } : {})}
        />
      ) : resolving ? (
        <ChatFramePlaceholder />
      ) : (
        <p
          className={
            "task-card-starting" + (emptyPaneFailed(task, gone) ? " is-missing" : "")
          }
        >
          {emptyPaneText(task, gone)}
        </p>
      )}
    </Modal>
      {erasing && (
        <EraseTaskModal
          task={task}
          onClose={() => setErasing(false)}
          onDone={() => {
            setErasing(false);
            notify({ title: `Deleted ${task.task_id}`, tone: "info", tier: "trail" });
            onReload?.();
            onClose();
          }}
        />
      )}
    </>
  );
}

// The head's icons, in MenuIcons' own stroke (platform/ui/MenuIcons: 16px,
// 24-grid, 1.5 stroke, round joins) so they sit in the app's buttons at the
// weight its menus draw. Inline rather than added to that record because they
// are this popup's and nothing else's — the folder.
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

// Archive and Unarchive are the List row's own glyphs (ScheduleTaskViews:
// lucide `archive` / `archive-restore`), imported, not redrawn: the same box a
// reader learned on the row is the box on the card and in the popup (Akshil,
// 2026-09-06: "why is it different from the list unarchive icon").
