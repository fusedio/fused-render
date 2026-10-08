import type { Task } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { softNavigate } from "../strip";
import { taskHref } from "../../tasks-lib";
import { useOpenTasks } from "../data";
import { dimsOf, itemCapacity, type Widget } from "../layout";
import { BigCount, EmptyLine, ErrorLine, ItemList, ListSkeleton, MoreLine, type WidgetItem } from "./bits";

const TASKS_HREF = "/tasks";

type Lane = "queued" | "running" | "you";

/** Which board column a task sits in. `blocked` (a run that stopped and needs
    a human) reads as "Needs you" next to the permission/question cards. */
export function laneOf(t: Pick<Task, "status">): Lane {
  if (t.status === "in_progress") return "running";
  if (t.status === "needs_attention" || t.status === "blocked") return "you";
  return "queued";
}

const LANES: { id: Lane; label: string; empty: string }[] = [
  { id: "queued", label: "Queued", empty: "Nothing queued" },
  { id: "running", label: "In progress", empty: "Nothing running" },
  { id: "you", label: "Needs you", empty: "All clear" },
];

function toItem(t: Task): WidgetItem {
  const href = taskHref(t) ?? TASKS_HREF;
  const lane = laneOf(t);
  return {
    key: t.key,
    name: t.title || t.task_id,
    sub: `${t.task_id} · ${basename(t.project || t.target || "")}`,
    href,
    pill:
      lane === "you"
        ? { label: "Needs you", tone: "warn" }
        : lane === "running"
          ? { label: "Running", tone: "ok" }
          : { label: "Queued", tone: "idle" },
  };
}

export function TasksWidget({ widget }: { widget: Widget }) {
  const { data, error, retry } = useOpenTasks();
  if (error && !data) return <div className="hw-body"><ErrorLine message={`Couldn't load tasks. ${error}`} onRetry={retry} /></div>;
  if (!data) return <div className="hw-body"><ListSkeleton rows={3} label="Loading tasks" /></div>;
  if (!data.length) return <div className="hw-body"><EmptyLine>No open tasks.</EmptyLine></div>;
  const needs = data.filter((t) => laneOf(t) === "you").length;
  if (widget.format === "count") {
    return (
      <div className="hw-body">
        <BigCount
          value={String(data.length)}
          caption={data.length === 1 ? "open task" : "open tasks"}
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
            const rows = data.filter((t) => laneOf(t) === lane.id);
            const shown = rows.slice(0, cap);
            return (
              <div key={lane.id} className={`hw-lane is-${lane.id}`}>
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
