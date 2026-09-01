// The search row at the TOP of the Local tab: a query box, a task filter and a
// sort, above everything the page has to show about this machine.
//
// **At the top, because it is the one control that changes what the page IS.**
// The rest of the tab answers "what do I have and what should I get"; this row
// answers "what else is out there", and a query replaces the sections below it
// with one grid of Hub results (`HubResults`, the either/or in `LocalTab`).
// A control that swaps the whole page cannot sit halfway down it.
//
// Lifted out of the Discover tab, whose whole surface this was (D426). What
// moved is the machinery and not a second copy of it: the debounce, the settled
// object and the ✕ that clears BOTH inputs are the same pieces, now hanging off
// a page that already had the cards to draw the answers on.
//
// **The two menus are the app's own dropdown, not native selects** (D426). They
// were `<select className="field-control">`, which is the one control that
// renders as the PLATFORM's rather than as this app's — and, worse for these
// two, a control that cannot carry an icon. These menus are a vocabulary: the
// task filter and five orderings are the whole of what this row can be asked,
// and a row of glyphs is how a reader learns a vocabulary at a glance. So the
// trigger is a bordered button wearing the active option's icon, its label and a
// caret (the `.field-control` select skin's own metrics, so it sits in the row
// as a sibling of the search box), and the menu is `@platform/ui/ContextMenu` —
// the app's ONE menu surface, with `active` for radio semantics. Not the
// explorer's visually-closer ModeMenu: an app may not import another app's
// components (`scripts/check-boundaries.mjs`), and a third hand-rolled dropdown
// is exactly the drift the shared one exists to prevent.
//
// **This component is only the controls.** The state lives in `LocalTab`,
// because `settled` is what decides which face of the page is rendered and the
// ✕ here and the "← Back to models" control in the results heading are one act
// (`clearSearch`). What IS this component's is the task glossary, which nothing
// else on the page reads, and which menu is open.
import { useEffect, useState, type ReactNode, type RefObject } from "react";
import { ChevronDownIcon } from "lucide-react";
import {
  activeSort,
  activeTask,
  SORTS,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
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
import { MenuIcons } from "@platform/ui/MenuIcons";

/** Which glyph each ordering wears. Beside the table rather than in it because
 *  `hubSearchView` is a `.ts` module that can be unit-tested and an icon is JSX
 *  — the part with a rule in it is which sorts exist and what they mean, and it
 *  stays there.
 *
 *  `downloads` reuses the shared `download` glyph (an arrow into a tray) rather
 *  than getting one of its own: that is exactly what a download count counts.
 *  `updated` does NOT reuse `refresh`, whose two circular arrows already mean
 *  "fetch this again" everywhere else in the app — this is a fact about the
 *  repo, not an action. */
const SORT_ICONS: Record<ResultSort, ReactNode> = {
  downloads: MenuIcons.download,
  likes: MenuIcons.heart,
  updated: MenuIcons.clock,
  created: MenuIcons.sparkle,
  size: MenuIcons.drive,
};

/** One of the row's two menus: a bordered trigger showing what is in force, and
 *  the app's dropdown hanging off it. Radio semantics — each menu is one
 *  exclusive choice — so the items are a `DropdownMenuRadioGroup`. */
function ControlMenu({
  icon,
  label,
  title,
  ariaLabel,
  value,
  onValue,
  items,
  leading,
}: {
  icon: ReactNode;
  label: string;
  title: string;
  ariaLabel: string;
  value: string;
  onValue: (v: string) => void;
  items: { value: string; label: string; icon: ReactNode }[];
  /** Rendered above a separator, part of the same radio group ("Any task"). */
  leading?: { value: string; label: string; icon: ReactNode };
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={ariaLabel}
            title={title}
          />
        }
      >
        <span aria-hidden="true">{icon}</span>
        {label}
        <ChevronDownIcon />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuRadioGroup value={value} onValueChange={(v) => onValue(v as string)}>
          {leading && (
            <>
              <DropdownMenuRadioItem value={leading.value}>
                {leading.icon}
                {leading.label}
              </DropdownMenuRadioItem>
              {items.length > 0 && <DropdownMenuSeparator />}
            </>
          )}
          {items.map((it) => (
            <DropdownMenuRadioItem key={it.value} value={it.value}>
              {it.icon}
              {it.label}
            </DropdownMenuRadioItem>
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
   *  settled query, because it belongs to the box: appearing 350ms after the
   *  first keystroke, or lingering that long after a clear, is the control
   *  disagreeing with the field it sits in. Everything else on the page
   *  describes what is RENDERED and waits for the debounce. */
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
    // The filter list is small and comes from the server because only the
    // server knows which pipeline tags a registered runner can serve (D313) —
    // a hardcoded menu here would offer filters for models the app cannot load,
    // which is the whole complaint this constraint answers. It is a static
    // glossary and touches no network (`hub/tasks` is a GET over a table), so
    // asking for it when the tab opens does not make this page reach the Hub
    // before something is typed. A failure is not worth a banner: the search
    // still works, it just has no task menu.
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

  // ONE glyph for the whole task group, and that is a choice rather than a gap.
  // A per-task icon would have to be invented for every pipeline tag the server
  // registers — and the tags are the server's, not this file's (HS-7), so the
  // next runner someone registers would arrive iconless or, worse, get an
  // arbitrary glyph that reads as a claim about what it does. The funnel says
  // the true thing about every row in this menu: it narrows the results.
  const taskItems = tasks.map((t) => ({
    value: t.tag,
    label: t.label,
    icon: MenuIcons.filter,
  }));

  const sortItems = SORTS.map((s) => ({
    value: s.value,
    label: s.label,
    icon: SORT_ICONS[s.value],
  }));

  return (
    <div className="am-hub-controls">
      <div className="am-hub-field">
        <input
          ref={searchBox}
          className="am-hub-search"
          type="search"
          value={query}
          placeholder="Search models on the Hub…"
          aria-label="Search models on the Hugging Face Hub"
          onChange={(e) => onQuery(e.target.value)}
          // Escape is the reflex for "put this back", and in this box it
          // clears the TASK FILTER too — the same one act the ✕ performs, for
          // the same reason. Not stopPropagation: nothing else on this page
          // listens for Escape while a text field has focus, and swallowing
          // it would break the next overlay that does. (An OPEN menu does
          // listen, on document capture, and stops the key there — so Escape
          // dismisses the menu first and clears the search second, which is
          // the order a reader expects of the thing most recently opened.)
          onKeyDown={(e) => {
            if (e.key !== "Escape" || !showsReset) return;
            e.preventDefault();
            onClear();
          }}
        />
        {/* Inside the box, and it clears BOTH inputs. The native type="search"
            ✕ is hidden in CSS precisely because it does not: it empties the
            text and leaves a task filter behind, which is the exact failure
            that looks broken — the box is empty, the reader has done the
            obvious thing, and the models still are not back (D317). */}
        {showsReset && (
          <button
            type="button"
            className="am-hub-clear"
            onClick={onClear}
            aria-label="Clear the search and show this machine's models"
            title="Clear the search and the task filter (Esc)"
          >
            ✕
          </button>
        )}
      </div>
      <ControlMenu
        icon={MenuIcons.filter}
        label={activeT.label}
        title={activeT.title}
        ariaLabel={"Filter by task: " + activeT.label}
        value={task.trim()}
        onValue={onTask}
        // "Any task" means any task THIS APP RUNS — the menu holds only
        // those (D313), and so does an unfiltered search.
        leading={{ value: "", label: "Any task", icon: MenuIcons.filter }}
        items={taskItems}
      />
      <ControlMenu
        icon={SORT_ICONS[activeS.value]}
        label={activeS.label}
        title={activeS.title}
        ariaLabel={"Sort results: " + activeS.label}
        value={sort}
        onValue={(v) => onSort(v as ResultSort)}
        items={sortItems}
      />
    </div>
  );
}
