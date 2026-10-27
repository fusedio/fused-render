import type { Task } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { TaskRowItem } from "@shell/ScheduleTaskViews";
import { softNavigate } from "../strip";
import { liveJobs, taskHref } from "../../tasks-lib";
import { useHomeTasks, type HomeTasks } from "../data";
import { dimsOf, itemCapacity, type TasksShow, type Widget } from "../layout";
import { BigCount, EmptyLine, ErrorLine, ListSkeleton, MoreLine, useFitCount, type WidgetItem } from "./bits";

const TASKS_HREF = "/tasks";

type Lane = "queued" | "running" | "you" | "done";

type Row = Pick<Task, "status" | "background_jobs">;

/** A finished turn whose chat still holds a live background job (tasks-lib
    .liveJobRow, Akshil 2026-10-18): drawn In Progress, not Done, while the API
    status stays `done`. */
function backgroundHeld(t: Row): boolean {
  return t.status === "done" && liveJobs(t) !== null;
}

/** Done, and nothing still running under it. */
function settled(t: Row): boolean {
  return t.status === "done" && !backgroundHeld(t);
}

/** Which board column a task sits in. `blocked` (a run that stopped and needs
    a human) reads as "Needs you" next to the permission/question cards. */
export function laneOf(t: Row): Lane {
  if (backgroundHeld(t)) return "running";
  if (t.status === "done") return "done";
  if (t.status === "in_progress") return "running";
  if (t.status === "needs_attention" || t.status === "blocked") return "you";
  return "queued";
}

const LANES: { id: Lane; label: string; empty: string }[] = [
  { id: "queued", label: "Queued", empty: "Nothing queued" },
  { id: "running", label: "In progress", empty: "Nothing running" },
  { id: "you", label: "Needs you", empty: "All clear" },
  { id: "done", label: "Done", empty: "Nothing done yet" },
];

/** Lanes drawn for a tile: the Done lane only when done tasks are shown. */
export function lanesFor(show: TasksShow) {
  return show === "open" ? LANES.filter((l) => l.id !== "done") : LANES;
}

/** List rows for a tile: open first, then done unless the tile shows open only. */
export function listFor<T extends Row>(tasks: T[], show: TasksShow): T[] {
  const ordered = listOrder(tasks);
  return show === "open" ? ordered.filter((t) => !settled(t)) : ordered;
}

/** Open = not done (archived and drafts never reach the widget). */
export function openCount(tasks: Row[]): number {
  return tasks.filter((t) => !settled(t)).length;
}

/** Why the widget is empty: nothing at all, or only drafts and archived tasks. */
export function emptyMessage({ drafts, archived }: Pick<HomeTasks, "drafts" | "archived">): string {
  const parts = [
    drafts ? `${drafts} draft${drafts === 1 ? "" : "s"}` : "",
    archived ? `${archived} archived` : "",
  ].filter(Boolean);
  return parts.length ? `No active tasks \u00b7 ${parts.join(", ")}` : "No tasks yet.";
}

/** List order: open tasks first, then done ones; each group keeps its incoming
    order — the Tasks page's own (`splitHomeTasks` → tasks-lib.sortForList),
    which already puts the done lane last, so this partition only moves a row
    whose lane and `settled` disagree. */
export function listOrder<T extends Row>(tasks: T[]): T[] {
  return [...tasks.filter((t) => !settled(t)), ...tasks.filter((t) => settled(t))];
}

export function taskPill(t: Pick<Task, "status" | "failed" | "background_jobs">): NonNullable<WidgetItem["pill"]> {
  if (backgroundHeld(t)) return { label: "Background", tone: "ok" };
  switch (t.status) {
    case "in_progress":
      return { label: "Running", tone: "ok" };
    case "needs_attention":
    case "blocked":
      return { label: "Needs you", tone: "warn" };
    case "done":
      return t.failed ? { label: "Failed", tone: "err" } : { label: "Done", tone: "idle" };
    default:
      return { label: "Queued", tone: "idle" };
  }
}

function toItem(t: Task): WidgetItem {
  const href = taskHref(t) ?? TASKS_HREF;
  return {
    key: t.key,
    name: t.title || t.task_id,
    sub: `${t.task_id} · ${basename(t.project || t.target || "")}`,
    href,
    pill: taskPill(t),
  };
}

/** The list format: the Tasks page's own rows (`TaskRowItem` in the page's
    `.tasks-list-frame`), as many as the tile's height holds — a list never
    grows the tile. No "+N more" line: the header's See all is the way to the
    rest, so that space goes to rows. */
function TaskRows({ rows, cap, onReload }: { rows: Task[]; cap: number; onReload: () => void }) {
  const { ref, n } = useFitCount(rows.length, cap, ".tasks-node", undefined, { more: false });
  const shown = rows.slice(0, n);
  return (
    <div ref={ref} className="hw-task-rows">
      <div className="tasks-list-frame">
        {shown.map((t) => {
          const href = taskHref(t) ?? TASKS_HREF;
          return <TaskRowItem key={t.key} task={t} href={href} onPress={() => navigateUrl(href)} onReload={onReload} />;
        })}
      </div>
    </div>
  );
}

/** One board column. It shows as many cards as its height holds and clips the rest, so a lane never grows the tile. */
function BoardLane({ lane, rows, cap }: { lane: { id: Lane; label: string; empty: string }; rows: Task[]; cap: number }) {
  const { ref, n, moreFits } = useFitCount(rows.length, cap, ".hw-lane-card");
  const shown = rows.slice(0, n);
  return (
    <div ref={ref} className={`hw-lane is-${lane.id}`}>
      <div className="hw-lane-head">
        <span className="hw-lane-dot" aria-hidden="true" />
        <span className="hw-lane-label">{lane.label}</span>
        <span className="hw-lane-n">{rows.length}</span>
      </div>
      {shown.length === 0 && <div className="hw-lane-empty">{lane.empty}</div>}
      {shown.map((t) => {
        const { href, name, sub } = toItem(t);
        return (
          <a key={t.key} className="hw-lane-card" href={href} title={name} onClick={(e) => softNavigate(e, href!)}>
            <span className="hw-lane-title">{name}</span>
            <span className="hw-lane-meta">{sub}</span>
          </a>
        );
      })}
      <MoreLine count={moreFits ? rows.length - shown.length : 0} href={TASKS_HREF} />
    </div>
  );
}

export function TasksWidget({ widget }: { widget: Widget }) {
  const { data: split, error, retry, refresh } = useHomeTasks();
  if (error && !split) return <div className="hw-body"><ErrorLine message={`Couldn't load tasks. ${error}`} onRetry={retry} /></div>;
  if (!split) return <div className="hw-body"><ListSkeleton rows={3} label="Loading tasks" /></div>;
  const data = split.tasks;
  const show: TasksShow = widget.show ?? "open_done";
  if (!data.length) return <div className="hw-body"><EmptyLine>{emptyMessage(split)}</EmptyLine></div>;
  const open = openCount(data);
  const needs = data.filter((t) => laneOf(t) === "you").length;
  if (widget.format === "count") {
    return (
      <div className="hw-body">
        <BigCount
          value={String(open)}
          caption={open === 1 ? "open task" : "open tasks"}
          accent={needs ? `${needs} need${needs === 1 ? "s" : ""} you` : undefined}
        />
      </div>
    );
  }
  if (widget.format === "board") {
    // First-paint guess only; each lane measures how many cards its tile really holds.
    const cap = dimsOf(widget).rows >= 4 ? 4 : 2;
    const lanes = lanesFor(show);
    return (
      <div className="hw-body">
        <div className="hw-board" style={{ "--hw-lanes": lanes.length } as React.CSSProperties}>
          {lanes.map((lane) => (
            <BoardLane key={lane.id} lane={lane} rows={data.filter((t) => laneOf(t) === lane.id)} cap={cap} />
          ))}
        </div>
      </div>
    );
  }
  const rows = listFor(data, show);
  if (!rows.length) return <div className="hw-body"><EmptyLine>Nothing open</EmptyLine></div>;
  return (
    <div className="hw-body">
      <TaskRows rows={rows} cap={itemCapacity(widget.size, "list")} onReload={refresh} />
    </div>
  );
}
