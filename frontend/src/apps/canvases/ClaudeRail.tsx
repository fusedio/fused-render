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
//   - at the bottom, this canvas's Tasks with a running count.
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { ListTodo, Plus } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { getSchedule, getTasks, type Task, type TaskPulseTask } from "@platform/lib/api";
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
import NewJobModal from "@shell/NewJobModal";
import { onRail, railRows } from "./claude-rail-lib";
import type { WorkbenchEnv } from "./api";

/** A chat ring's state IN WORDS, for its accessible name (CodeRabbit on
 *  #1971): the ring says it in colour and fill only, and the link's
 *  `aria-label` replaces its content, so the ring's own label never reaches a
 *  screen reader. Same precedence as the ring: a background job, then a run
 *  waiting on the reader, then one in flight; settled rows say their lane and
 *  whether they are unread. */
function statusWords(t: TaskPulseTask, failed: boolean): string {
  if (liveJobRow(t)) return "running in the background";
  const column = taskColumn(t);
  if (column === "needs_attention") return "waiting for you";
  if (inFlight(column)) return "running";
  const base = failed ? "blocked" : String(column).replace(/_/g, " ");
  return t.unread > 0 ? `${base}, unread` : base;
}

/** What the New task card needs from the schedule before it can open: the
 *  permission modes the server offers and the folders existing tasks point at
 *  (NewJobModal `permissionModes`, `recentTargets` — what Scheduled.tsx hands
 *  it off the schedule it already holds; the rail has no schedule, so it asks
 *  on the press). `seq` keys the mount: every press is a fresh card. */
interface Card {
  seq: number;
  modes: string[];
  recents: string[];
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
   *  project; the New task card's locked target). Null until the clone
   *  exists. */
  dir: string | null;
  /** The session the page's URL names, drawn as the current row. */
  activeSessionId: string | null;
  tasksHref: string;
  /** Open the pane as it is. */
  onOpen: () => void;
  /** Open one of the canvas's chats: its Tasks-page door (`taskHref`). */
  onOpenChat: (href: string) => void;
  onTasks: () => void;
  /** A pointer down on the rail's inner edge: the start of a pull that opens
   *  the pane at the width the pointer drags it to (CanvasWorkspace
   *  `onRailPull`). */
  onPullStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
}) {
  const rows = useTasksPulseRows();
  const place = useMemo(() => ({ env, name, dir }), [env, name, dir]);
  // This canvas's live chats, unordered — the signature below is what decides
  // when the listing is re-read; the order is `railRows`'s, once it is held.
  const mine = useMemo(() => rows.filter((t) => onRail(t, place)), [rows, place]);

  // THE CAPTION'S WORDS ARE THE LISTING'S, not the pulse's: the pulse carries
  // status and a title, but the list row's caption is built from the full row
  // (recap, last reply, last message) — and so is the List's ORDER
  // (sortForList reads the full row). Seeded from the listing the Tasks page
  // already holds, and re-read whenever one of THIS canvas's rows moves —
  // keyed on what changes a caption or a rank, so an idle canvas never asks
  // again.
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  const sig = mine.map((t) => `${t.key}:${t.status}:${t.last_active}:${t.unread}`).join("|");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const refreshListing = useCallback(
    () =>
      getTasks()
        .then((r) => {
          if (!alive.current) return;
          rememberListing(r.tasks);
          setListing(r.tasks);
        })
        .catch(() => undefined),
    [],
  );
  useEffect(() => {
    if (!sig) return;
    let off = false;
    const timer = window.setTimeout(() => {
      if (!off) void refreshListing();
    }, 600);
    return () => {
      off = true;
      window.clearTimeout(timer);
    };
  }, [sig, refreshListing]);
  const full = useMemo(() => new Map((listing ?? []).map((t) => [t.key, t])), [listing]);

  // THE RAIL'S SEATS, in the List's order (claude-rail-lib). `now` is read
  // once per derivation, as `sortForList` asks; the derivation re-runs when a
  // row moves (the pulse) or the listing re-reads, which is when a rank can
  // change.
  const seats = useMemo(() => railRows(rows, full, place), [rows, full, place]);

  const running = mine.filter((t) => liveJobRow(t) || inFlight(taskColumn(t))).length;

  // THE NEW TASK CARD. Opened on the press, once the schedule has answered
  // what the card needs (`Card`); the press is inert while that read is out,
  // so a double-click cannot stack two cards. A schedule read that fails
  // still opens the card — the modal falls back to "auto" on an empty modes
  // list, and an empty recents list only means fewer suggestions — because a
  // + that does nothing is worse than a card with one fewer dropdown option.
  const [card, setCard] = useState<Card | null>(null);
  const [opening, setOpening] = useState(false);
  const openNewTask = () => {
    if (opening || card) return;
    setOpening(true);
    void getSchedule()
      .then(
        (s) => ({ modes: s.permission_modes, recents: s.entries.map((e) => e.target) }),
        () => ({ modes: [] as string[], recents: [] as string[] }),
      )
      .then((got) => {
        if (!alive.current) return;
        setOpening(false);
        setCard({ seq: Date.now(), ...got });
      });
  };

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
        onClick={onOpen}
      >
        <PanelIcon side="right" />
      </button>
      <div className="sidebar-rail-items">
        <button
          type="button"
          className="sidebar-rail-btn canvas-rail-item"
          title="New task"
          aria-label="New task"
          aria-haspopup="dialog"
          aria-busy={opening || undefined}
          disabled={opening}
          onClick={openNewTask}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        {seats.length > 0 && (
          <div className="sidebar-rail-scroll canvas-rail-chats" role="list" aria-label="Chats on this canvas">
            {seats.map(({ pulse: t, row }) => {
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
                  aria-label={`${label} — ${statusWords(t, !!row?.failed)}`}
                  // The list row's own caption (platform/lib/hints): the
                  // title's line, then the reply's — what the row clamps.
                  data-hint={line.hint || label}
                  data-hint-title={line.hint || label}
                  data-hint-reply={reply}
                  // Beside the ring, not at the pointer (platform/lib/hints
                  // `placeBeside`): a 48px column at the window's edge.
                  data-hint-side="left"
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
        <span className="canvas-rail-spacer" />
        <a
          className="sidebar-rail-btn canvas-rail-item"
          href={tasksHref}
          title={running > 0 ? `Tasks on this canvas · ${running} running` : "Tasks on this canvas"}
          aria-label={running > 0 ? `Tasks on this canvas, ${running} running` : "Tasks on this canvas"}
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
      {card && (
        <NewJobModal
          // Keyed on the press: the card reads its props in `useState`
          // initialisers, so every opening must be a fresh mount
          // (Scheduled.tsx `openSeq`).
          key={card.seq}
          initialTime={null}
          // SCOPED TO THE CLONE, the project page's Tasks tab's way
          // (CanvasPage.tsx `scope.project`, Scheduled.tsx `lockTarget`): a
          // task made from the canvas is a task about the canvas, so the path
          // is stated, not asked. Until the clone exists the card asks.
          initialTarget={dir}
          lockTarget={dir !== null}
          // From the List, not the calendar: a task typed here is one to run
          // now, and the when-row folds into More options.
          planning={false}
          permissionModes={card.modes}
          recentTargets={card.recents}
          onClose={() => setCard(null)}
          // "Something changed, re-read": the pulse will move the new row
          // onto the rail; the listing is re-read now so its caption and
          // rank are not 600ms behind its ring.
          onCreated={() => void refreshListing()}
        />
      )}
    </aside>
  );
}
