import { ListTodo } from "lucide-react";
import { TASK_ACTION_LABELS, openInTasksHref } from "@platform/lib/task-actions";

/** The ⋮ row "Open in Tasks" (a `KebabExtraItem`'s shape), or null when it
 *  does not apply — see `openInTasksHref`. `onOpen` is the host's own way of
 *  going to a page (its door's route, and whatever it does to close after). */
export function openInTasksItem(opts: {
  on: boolean;
  taskKey: string | null | undefined;
  pathname?: string;
  onOpen: (href: string) => void;
}): { label: string; icon: React.ReactNode; onClick: () => void } | null {
  const href = openInTasksHref({
    on: opts.on,
    taskKey: opts.taskKey,
    pathname: opts.pathname ?? (typeof location === "undefined" ? "" : location.pathname),
  });
  if (!href) return null;
  return {
    label: TASK_ACTION_LABELS.openInTasks,
    icon: <ListTodo size={16} strokeWidth={1.6} aria-hidden="true" />,
    onClick: () => opts.onOpen(href),
  };
}

/** `<Kebab {...openInTasksProps(item)} />`: the row as `extraItems`, or no prop. */
export function openInTasksProps<T>(item: T | null): { extraItems?: T[] } {
  return item ? { extraItems: [item] } : {};
}
