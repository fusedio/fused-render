// THE CLAUDE RAIL: what stands at the canvas page's right edge while the Claude
// pane is shut (owner, 2026-10-26: "a very min width column, like we have for
// the fused render left sidebar, just a single width column with the button";
// "rail stays on only when claude panel is collapsed").
//
// The same 48px column the global sidebar collapses to, in the same vocabulary
// (sidebar.css: .sidebar-rail-btn, .sidebar-rail-items, .sidebar-rail-scroll),
// so the two edges of the window read as one idea. Top to bottom:
//   - the panel glyph (platform/ui/PanelIcon) — the way back IN. Its other
//     half, the Hide, is the first seat of the chat's own top row while the
//     pane is up (ClaudeChat `onHidePane`): exactly one of the two is on
//     screen at any moment, the explorer sidebar's own rule (SideChrome.tsx).
//   - EVERY CHAT ON THIS CANVAS, newest first (owner, 2026-10-27: "show all
//     chats we have here, and when we hover we see recap, title — whatever we
//     show in list view"). One status ring each — the Tasks page's own
//     (`StatusIcon`: lane colour, unread fill, "!" when it is waiting on you) —
//     and the hover is the list row's own caption: the same `data-hint-title`
//     / `data-hint-reply` the row's title carries (platform/lib/hints), built
//     from the same `cardTitleLine`. A press opens that chat where the Tasks
//     page's door would (`taskHref`): the canvas chat on its session, or the
//     node selected with its chat on its session. Scrolls on a short window.
//   - New chat. Alone, under the Show, when the canvas has no chats yet.
//   - at the bottom, this canvas's Tasks with a running count.
import { useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";
import { ListTodo, Plus } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { getTasks, type Task, type TaskPulseTask } from "@platform/lib/api";
import { readListing, rememberListing, useTasksPulseRows } from "@shell/tasksPulse";
import {
  cardTitleLine,
  inFlight,
  liveJobRow,
  recapTitlesOn,
  taskColumn,
  taskHref,
} from "@shell/tasks-lib";
import { StatusIcon } from "@shell/ScheduleTaskViews";
import type { WorkbenchEnv } from "./api";

export type RailTarget = "show" | "new";

/** A row's time, newest first. Anything unparseable sorts last. */
function when(t: Pick<TaskPulseTask, "last_active">): number {
  const ms = Date.parse(String(t.last_active ?? ""));
  return Number.isFinite(ms) ? ms : 0;
}

export function ClaudeRail({
  env,
  name,
  dir,
  activeSessionId,
  tasksHref,
  onOpen,
  onOpenChat,
  onTasks,
  onPullStart,
}: {
  env: WorkbenchEnv;
  name: string;
  /** The clone dir (task rows that predate `task.canvas` name it as their
   *  project). Null until the clone exists. */
  dir: string | null;
  /** The session the page's URL names, drawn as the current row. */
  activeSessionId: string | null;
  tasksHref: string;
  /** Open the pane as it is ("show"), or on a fresh canvas chat ("new"). */
  onOpen: (target: RailTarget) => void;
  /** Open one of the canvas's chats: its Tasks-page door (`taskHref`). */
  onOpenChat: (href: string) => void;
  onTasks: () => void;
  /** A pointer down on the rail's inner edge: the start of a pull that opens
   *  the pane at the width the pointer drags it to (CanvasWorkspace
   *  `onRailPull`). */
  onPullStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
}) {
  const rows = useTasksPulseRows();
  // This canvas's chats — the ones that have a conversation to open. A row
  // that predates `task.canvas` is matched by its project (the clone dir).
  const mine = useMemo(
    () =>
      rows
        .filter(
          (t) =>
            !!t.session_id &&
            ((t.canvas && t.canvas.env === env && t.canvas.name === name) ||
              (dir !== null && t.project === dir)),
        )
        .sort((a, b) => when(b) - when(a)),
    [rows, env, name, dir],
  );

  // THE CAPTION'S WORDS ARE THE LISTING'S, not the pulse's: the pulse carries
  // status and a title, but the list row's caption is built from the full row
  // (recap, last reply, last message). Seeded from the listing the Tasks page
  // already holds, and re-read whenever one of THIS canvas's rows moves —
  // keyed on what changes a caption, so an idle canvas never asks again.
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  const sig = mine.map((t) => `${t.key}:${t.status}:${t.last_active}:${t.unread}`).join("|");
  useEffect(() => {
    if (!sig) return;
    let off = false;
    const timer = window.setTimeout(() => {
      void getTasks()
        .then((r) => {
          if (off) return;
          rememberListing(r.tasks);
          setListing(r.tasks);
        })
        .catch(() => undefined);
    }, 600);
    return () => {
      off = true;
      window.clearTimeout(timer);
    };
  }, [sig]);
  const full = useMemo(() => new Map((listing ?? []).map((t) => [t.key, t])), [listing]);

  const running = mine.filter((t) => liveJobRow(t) || inFlight(taskColumn(t))).length;

  return (
    <aside className="canvas-rail" aria-label="Claude">
      {/* THE RAIL'S INNER EDGE IS A SEAM: pulled left it opens the pane at the
          width the pointer takes it to — the explorer sidebar's reopen edge
          (SideReopenEdge) and the global sidebar's rail, the same gesture on
          the same kind of edge. The Show button stays the click. */}
      <div
        className="canvas-rail-seam"
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
        onClick={() => onOpen("show")}
      >
        <PanelIcon side="right" />
      </button>
      <div className="sidebar-rail-items">
        {mine.length > 0 && (
          <div className="sidebar-rail-scroll canvas-rail-chats" role="list" aria-label="Chats on this canvas">
            {mine.map((t) => {
              const row = full.get(t.key);
              // The live status is the pulse's (fresher than the listing).
              const merged = row ? { ...row, status: t.status } : t;
              const line = cardTitleLine(merged);
              const reply = row && !recapTitlesOn(row) ? row.last_reply || "" : "";
              const label = line.text || t.title || "Chat";
              const href = taskHref(merged);
              if (!href) return null;
              return (
                <a
                  key={t.key}
                  role="listitem"
                  href={href}
                  className={
                    "sidebar-rail-btn canvas-rail-chat" +
                    (activeSessionId && t.session_id === activeSessionId ? " active" : "")
                  }
                  aria-label={label}
                  // The list row's own caption (platform/lib/hints): the
                  // title's line, then the reply's — what the row clamps.
                  data-hint={line.hint || label}
                  data-hint-title={line.hint || label}
                  data-hint-reply={reply}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    onOpenChat(href);
                  }}
                >
                  <StatusIcon
                    status={taskColumn(t)}
                    unread={t.unread > 0}
                    background={liveJobRow(t)}
                    {...(row?.failed ? { failed: true } : {})}
                  />
                </a>
              );
            })}
          </div>
        )}
        <button
          type="button"
          className="sidebar-rail-btn canvas-rail-item"
          title="New chat"
          aria-label="New chat"
          onClick={() => onOpen("new")}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        <span className="canvas-rail-spacer" />
        <a
          className="sidebar-rail-btn canvas-rail-item"
          href={tasksHref}
          title={running > 0 ? `Tasks on this canvas · ${running} running` : "Tasks on this canvas"}
          aria-label="Tasks on this canvas"
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
            e.preventDefault();
            onTasks();
          }}
        >
          <ListTodo size={16} aria-hidden="true" />
          {running > 0 && <span className="canvas-rail-count">{running}</span>}
        </a>
      </div>
    </aside>
  );
}
