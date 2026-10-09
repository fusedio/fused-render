import type { Task } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { softNavigate } from "../strip";
import { taskHref } from "../../tasks-lib";
import { useHomeTasks, type HomeTasks } from "../data";
import { dimsOf, itemCapacity, type TasksShow, type Widget } from "../layout";
import { BigCount, EmptyLine, ErrorLine, ItemList, ListSkeleton, MoreLine, useFitCount, type WidgetItem } from "./bits";

const TASKS_HREF = "/tasks";

type Lane = "queued" | "running" | "you" | "done";

/** Which board column a task sits in. `blocked` (a run that stopped and needs
    a human) reads as "Needs you" next to the permission/question cards. */
export function laneOf(t: Pick<Task, "status">): Lane {
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
export function listFor<T extends Pick<Task, "status">>(tasks: T[], show: TasksShow): T[] {
  const ordered = listOrder(tasks);
  return show === "open" ? ordered.filter((t) => t.status !== "done") : ordered;
}

/** Open = not done (archived and drafts never reach the widget). */
export function openCount(tasks: Pick<Task, "status">[]): number {
  return tasks.filter((t) => t.status !== "done").length;
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
    (newest-first) order. */
export function listOrder<T extends Pick<Task, "status">>(tasks: T[]): T[] {
  return [...tasks.filter((t) => t.status !== "done"), ...tasks.filter((t) => t.status === "done")];
}

export function taskPill(t: Pick<Task, "status" | "failed">): NonNullable<WidgetItem["pill"]> {
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

/** One board column. It shows as many cards as its height holds and clips the rest, so a lane never grows the tile. */
function BoardLane({ lane, rows, cap }: { lane: { id: Lane; label: string; empty: string }; rows: Task[]; cap: number }) {
  const { ref, n } = useFitCount(rows.length, cap, ".hw-lane-card");
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
      <MoreLine count={rows.length - shown.length} href={TASKS_HREF} />
    </div>
  );
}

export function TasksWidget({ widget }: { widget: Widget }) {
  const { data: split, error, retry } = useHomeTasks();
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
      <ItemList items={rows.map(toItem)} cap={itemCapacity(widget.size, "list")} moreHref={TASKS_HREF} />
    </div>
  );
}
