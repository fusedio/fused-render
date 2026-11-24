// The Task page's chat header names the pull requests the task is about: the
// newest as a `PR #N` link, the others behind a `+N` button that opens a list
// (with each one's GitHub status once known). Lives in platform/ because the
// chat (apps/claude) renders it itself for the explorer's pane; the shell's
// TaskPrChip is a re-export for the hosts that pass it as `headerExtra`.
import { GitPullRequest } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { usePrStatuses } from "@platform/lib/prStatus";
import { prKey, prRefsOf, prUrl, type PrRef } from "@platform/lib/task-prs";
import "./task-pr-dot.css";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";

/** The "other pull requests" list. Mounted only while the popover is open
 *  (Base UI unmounts closed content), so GitHub is asked about these only when
 *  somebody looks. */
export function PrPopList({ refs }: { refs: PrRef[] }) {
  const statuses = usePrStatuses(refs.map(prKey));
  return (
    <ul>
      {refs.map((r) => {
        const state = statuses[prKey(r)];
        return (
          <li key={prKey(r)}>
            {state ? <span className="task-pr-dot" data-status={state} title={state} /> : null}
            <a href={prUrl(r) ?? undefined} target="_blank" rel="noopener noreferrer">
              {r.repo} #{r.number}
            </a>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * THE NEWEST PULL REQUEST IS ALWAYS THE TAG (Akshil, 2026-11-19: "PR should
 * have higher priority on visibility in all these views") — the branch tag
 * beside it gives way instead (shown whole or not at all), so the 2026-11-17
 * `collapsed` form (every pull request behind one "+N" beside a pane's
 * tools) is gone.
 *
 * In the shared task head the tag is a pull-request glyph and the number:
 * `.task-head-chips` shows the glyph and hides the word. Everywhere else (the
 * old slim line) it reads "PR #12" as it always did.
 */
export function TaskPrChip({ task }: { task: Task }) {
  const refs = prRefsOf(task).filter((r) => r.repo);
  if (!refs.length) return null;
  const head = refs[0];
  const rest = refs.slice(1);
  const href = prUrl(head) ?? undefined;
  return (
    <span className="task-pr-chips">
      <a
        className="task-pr-chip"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={href}
        aria-label={`Open pull request ${head.number} on GitHub`}
      >
        <GitPullRequest className="task-pr-glyph" size={12} aria-hidden="true" />
        <span className="task-pr-label">
          <span className="task-pr-word">PR </span>#{head.number}
        </span>
      </a>
      {rest.length ? (
        <Popover>
          <PopoverTrigger
            render={<button type="button" className="task-pr-more" aria-label={`${rest.length} more pull requests`} />}
          >
            +{rest.length}
          </PopoverTrigger>
          <PopoverContent className="task-pr-pop" align="start">
            <PrPopList refs={rest} />
          </PopoverContent>
        </Popover>
      ) : null}
    </span>
  );
}
