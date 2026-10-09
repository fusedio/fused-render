import type { Task } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { softNavigate } from "../strip";
import { isDraftTask, taskHref } from "../../tasks-lib";
import { BOARD_LANES, laneOf, type BoardLane } from "../../schedule-lib";
import { useHomeTasks } from "../data";
import { dimsOf, itemCapacity, type Widget } from "../layout";
import { BigCount, EmptyLine, ErrorLine, ItemList, ListSkeleton, MoreLine, type WidgetItem } from "./bits";

const TASKS_HREF = "/tasks";

const LANES = BOARD_LANES.filter((l) => l.key !== "archived") as readonly { key: BoardLane; label: string }[];

const LANE_EMPTY: Record<BoardLane, string> = {
  upcoming: "Nothing upcoming",
  in_progress: "Nothing running",
  blocked: "All clear",
  done: "Nothing done yet",
  archived: "",
};

/** Which Tasks-page board lane a task sits in; a draft draws in Upcoming. */
export function taskLane(t: Pick<Task, "status" | "kind">): BoardLane {
  return laneOf(isDraftTask(t) ? "draft" : t.status);
}

/** Open = not done and not a draft (archived rows never reach the widget). */
export function openCount(tasks: Pick<Task, "status" | "kind">[]): number {
  return tasks.filter((t) => t.status !== "done" && t.status !== "archived" && !isDraftTask(t)).length;
}

export function taskPill(t: Pick<Task, "status" | "kind" | "failed">): NonNullable<WidgetItem["pill"]> {
  if (isDraftTask(t)) return { label: "Draft", tone: "idle" };
  switch (t.status) {
    case "in_progress":
      return { label: "Running", tone: "ok" };
    case "queued":
      return { label: "Queued", tone: "idle" };
    case "needs_attention":
    case "blocked":
      return { label: "Needs you", tone: "warn" };
    case "done":
      return t.failed ? { label: "Failed", tone: "err" } : { label: "Done", tone: "idle" };
    default:
      return { label: "Upcoming", tone: "idle" };
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

export function TasksWidget({ widget }: { widget: Widget }) {
  const { data, error, retry } = useHomeTasks();
  if (error && !data) return <div className="hw-body"><ErrorLine message={`Couldn't load tasks. ${error}`} onRetry={retry} /></div>;
  if (!data) return <div className="hw-body"><ListSkeleton rows={3} label="Loading tasks" /></div>;
  if (!data.length) return <div className="hw-body"><EmptyLine>No tasks yet.</EmptyLine></div>;
  const open = openCount(data);
  const needs = data.filter((t) => taskLane(t) === "blocked").length;
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
    const cap = widget.size === "2x2" || dimsOf(widget).rows >= 4 ? 4 : 2;
    return (
      <div className="hw-body">
        <div className="hw-board">
          {LANES.map((lane) => {
            const rows = data.filter((t) => taskLane(t) === lane.key);
            const shown = rows.slice(0, cap);
            return (
              <div key={lane.key} className={`hw-lane is-${lane.key}`}>
                <div className="hw-lane-head">
                  <span className="hw-lane-dot" aria-hidden="true" />
                  <span className="hw-lane-label">{lane.label}</span>
                  <span className="hw-lane-n">{rows.length}</span>
                </div>
                {shown.length === 0 && <div className="hw-lane-empty">{LANE_EMPTY[lane.key]}</div>}
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
          })}
        </div>
      </div>
    );
  }
  return (
    <div className="hw-body">
      <ItemList items={data.map(toItem)} cap={itemCapacity(widget.size, "list")} moreHref={TASKS_HREF} />
    </div>
  );
}
