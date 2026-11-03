// The Workspace's search-and-resume palette (⌘K and `/`): every task, open or
// closed, by title or T-number. Enter on an open task opens it; Enter on a
// closed one brings it back first, then opens it. The ordering (open tasks
// before closed ones) and the match rule are `searchTasks`.
import { useEffect, useMemo, useRef, useState } from "react";
import type { Task } from "@platform/lib/api";
import { searchTasks, statusWord } from "@shell/workspace-lib";

const MAX_RESULTS = 50;

export function WorkspaceSearch({
  tasks,
  onPick,
  onClose,
}: {
  /** Every task, archived included (the page's unfiltered set). */
  tasks: readonly Task[];
  onPick: (task: Task) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const results = useMemo(() => searchTasks(tasks, query).slice(0, MAX_RESULTS), [tasks, query]);
  const active = Math.min(at, Math.max(0, results.length - 1));

  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  useEffect(() => {
    document
      .querySelector(".ws-search-item.is-active")
      ?.scrollIntoView?.({ block: "nearest" });
  }, [active, results]);

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setAt(Math.min(results.length - 1, active + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setAt(Math.max(0, active - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const pick = results[active];
      if (pick) onPick(pick);
    }
  };

  return (
    <div
      className="ws-search-scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="ws-search" role="dialog" aria-modal="true" aria-label="Search tasks" onKeyDown={onKeyDown}>
        <input
          ref={inputRef}
          className="ws-search-input"
          value={query}
          placeholder="Search tasks by title or number"
          aria-label="Search tasks"
          onChange={(e) => {
            setQuery(e.target.value);
            setAt(0);
          }}
        />
        <ul className="ws-search-list" role="listbox">
          {results.map((t, i) => (
            <li
              key={t.key}
              role="option"
              aria-selected={i === active}
              className={"ws-search-item" + (i === active ? " is-active" : "")}
              data-closed={t.status === "archived" ? "1" : undefined}
              onMouseMove={() => setAt(i)}
              onClick={() => onPick(t)}
            >
              <span className="ws-search-num">{t.task_id}</span>
              <span className="ws-search-title">{t.title}</span>
              <span className="ws-search-meta">
                {t.status === "archived" ? "closed · Enter reopens" : statusWord(t)}
              </span>
            </li>
          ))}
          {results.length === 0 && <li className="ws-search-empty">No task matches.</li>}
        </ul>
      </div>
    </div>
  );
}
