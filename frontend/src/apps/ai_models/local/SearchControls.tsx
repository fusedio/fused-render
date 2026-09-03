// The search row at the TOP of the Local tab: a query box, a task filter and a
// sort, above everything the page has to show about this machine.
//
// At the top because it is the one control that changes what the page IS: a
// query replaces the sections below it with one list of Hub results
// (`HubResults`, the either/or in `LocalTab`).
//
// The two menus are shadcn DropdownMenus with radio items — a vocabulary of a
// few orderings and task filters, each wearing its glyph so a reader learns it
// at a glance. The trigger is an outline button showing what is in force.
//
// This component is only the controls. The state lives in `LocalTab`, because
// `settled` decides which face of the page is rendered and the ✕ here and the
// "Back to models" control in the results heading are one act (`clearSearch`).
import { useEffect, useState, type ReactNode, type RefObject } from "react";
import { ChevronDownIcon, XIcon } from "lucide-react";
import { activeSort, activeTask, SORTS, type ResultSort } from "@apps/ai_models/lib/hubSearchView";
import { getHubTasks, type HubTask } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import { Input } from "@platform/shadcn/ui/input";
import { MenuIcons } from "@platform/ui/MenuIcons";

/** Which glyph each ordering wears. `downloads` reuses the shared download
 *  glyph — that is exactly what a download count counts. `updated` does NOT
 *  reuse `refresh`, which means "fetch this again" everywhere else. */
const SORT_ICONS: Record<ResultSort, ReactNode> = {
  downloads: MenuIcons.download,
  likes: MenuIcons.heart,
  updated: MenuIcons.clock,
  created: MenuIcons.sparkle,
  size: MenuIcons.drive,
};

interface Choice {
  value: string;
  label: string;
  icon: ReactNode;
}

/** One of the row's two menus: a bordered trigger showing what is in force, and
 *  a radio menu hanging off it. */
function ControlMenu({
  icon,
  label,
  title,
  ariaLabel,
  value,
  groups,
  onChange,
}: {
  icon: ReactNode;
  label: string;
  title: string;
  ariaLabel: string;
  value: string;
  /** Choices, separated between groups. */
  groups: Choice[][];
  onChange: (value: string) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" size="sm" aria-label={ariaLabel} title={title} />}>
        <span className="text-muted-foreground [&_svg]:size-3.5" aria-hidden="true">
          {icon}
        </span>
        {label}
        <ChevronDownIcon className="text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuRadioGroup value={value} onValueChange={(v) => onChange(String(v))}>
          {groups.map((group, gi) => (
            <div key={gi}>
              {gi > 0 && <DropdownMenuSeparator />}
              {group.map((c) => (
                <DropdownMenuRadioItem key={c.value} value={c.value}>
                  <span className="text-muted-foreground [&_svg]:size-3.5" aria-hidden="true">
                    {c.icon}
                  </span>
                  {c.label}
                </DropdownMenuRadioItem>
              ))}
            </div>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function SearchControls({
  query,
  task,
  sort,
  showsReset,
  searchBox,
  onQuery,
  onTask,
  onSort,
  onClear,
}: {
  query: string;
  task: string;
  sort: ResultSort;
  /** Whether the ✕ is offered — asked of the LIVE controls rather than of the
   *  settled query, because it belongs to the box. */
  showsReset: boolean;
  /** The page's handle on the input, so the control in the results heading can
   *  put the cursor back where the next thing happens. */
  searchBox: RefObject<HTMLInputElement>;
  onQuery: (q: string) => void;
  onTask: (task: string) => void;
  onSort: (sort: ResultSort) => void;
  /** Query AND task filter, in one act. See `clearSearch` in LocalTab. */
  onClear: () => void;
}) {
  const [tasks, setTasks] = useState<HubTask[]>([]);

  useEffect(() => {
    // The filter list comes from the server because only the server knows
    // which pipeline tags a registered runner can serve (D313). A static
    // glossary, no network. A failure is not worth a banner.
    let alive = true;
    getHubTasks().then(
      (d) => alive && setTasks(d.tasks),
      () => alive && setTasks([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  const activeT = activeTask(task, tasks);
  const activeS = activeSort(sort);

  // ONE glyph for the whole task group: the tags are the server's (HS-7), and
  // the funnel says the true thing about every row — it narrows the results.
  const taskGroups: Choice[][] = [
    [{ value: "", label: "Any task", icon: MenuIcons.filter }],
    ...(tasks.length ? [tasks.map((t) => ({ value: t.tag, label: t.label, icon: MenuIcons.filter }))] : []),
  ];
  const sortGroups: Choice[][] = [SORTS.map((s) => ({ value: s.value, label: s.label, icon: SORT_ICONS[s.value] }))];

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative min-w-64 flex-1">
        <Input
          ref={searchBox}
          // The native search ✕ is hidden on purpose: it empties the text and
          // leaves the task filter behind, which is the failure that looks
          // broken (D317). The ✕ beside it clears BOTH.
          className="pr-8 [&::-webkit-search-cancel-button]:appearance-none"
          type="search"
          value={query}
          placeholder="Search models on the Hub…"
          aria-label="Search models on the Hugging Face Hub"
          onChange={(e) => onQuery(e.target.value)}
          // Escape clears the TASK FILTER too — the same one act the ✕
          // performs. Not stopPropagation: an open menu listens on document
          // capture and stops the key there first.
          onKeyDown={(e) => {
            if (e.key !== "Escape" || !showsReset) return;
            e.preventDefault();
            onClear();
          }}
        />
        {showsReset && (
          <Button
            variant="ghost"
            size="icon-xs"
            className="absolute top-1 right-1"
            onClick={onClear}
            aria-label="Clear the search and show this machine's models"
            title="Clear the search and the task filter (Esc)"
          >
            <XIcon />
          </Button>
        )}
      </div>
      <ControlMenu
        icon={MenuIcons.filter}
        label={activeT.label}
        title={activeT.title}
        ariaLabel={"Filter by task: " + activeT.label}
        value={task.trim() ? task : ""}
        groups={taskGroups}
        onChange={onTask}
      />
      <ControlMenu
        icon={SORT_ICONS[activeS.value]}
        label={activeS.label}
        title={activeS.title}
        ariaLabel={"Sort results: " + activeS.label}
        value={sort}
        groups={sortGroups}
        onChange={(v) => onSort(v as ResultSort)}
      />
    </div>
  );
}
