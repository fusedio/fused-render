// The Task page's chat header names the pull request the task is about. The
// chat itself (apps/claude) may not import the shell, so the host builds this
// and hands it in as `ChatMount`'s `headerExtra`.
import type { Task } from "@platform/lib/api";
import { prRefOf } from "./tasks-lib";

export function TaskPrChip({ task }: { task: Task }) {
  const pr = prRefOf(task);
  if (!pr || !pr.repo) return null;
  const href = `https://github.com/${pr.repo}/pull/${pr.number}`;
  return (
    <a
      className="task-pr-chip"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={href}
      aria-label={`Open pull request ${pr.number} on GitHub`}
    >
      PR #{pr.number}
    </a>
  );
}
