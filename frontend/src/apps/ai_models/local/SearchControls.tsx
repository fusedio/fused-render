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
import {
  activeSort,
  activeTask,
  SORTS,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import { getHubTasks, type HubTask } from "@platform/lib/api";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
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
  // Trending — the share glyph: a repo trending is one being passed around
  // right now, the same fact `share` already names elsewhere in the app.
  trending: MenuIcons.share,
  // Fit — the info glyph: what this ordering adds over every other one is a
  // judgement ABOUT this machine, and info is this app's existing shorthand
  // for "here is a fact worth a second look" rather than a raw count.
  fit: MenuIcons.info,
  size: MenuIcons.drive,
};

/** The caret, drawn here rather than taken from `@platform/ui/Chevron` (which
 *  only points left/right — it is a pager's control). Same geometry, weight and
 *  muted colour as the one `select.field-control` paints as a background image,
 *  so a menu trigger and a select in the same row wear the same affordance. */
function Caret({ open }: { open: boolean }) {
  return (
    <svg
      className="am-hub-menu-caret"
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={open ? "M3 7.5l3-3 3 3" : "M3 4.5l3 3 3-3"} />
    </svg>
  );
}

/** One of the row's two menus: a bordered trigger showing what is in force, and
 *  the app's menu hanging off its bottom-left corner.
 *
 *  The open/close dance is the one thing worth reading twice. `ContextMenu`
 *  dismisses itself on any outside pointerdown, and the trigger is outside it —
 *  so a click on an open menu's own trigger would close it and then re-open it,
 *  leaving a control that cannot be dismissed by the obvious act. The toggle
 *  therefore lives on `pointerdown`, where the menu's own document-capture
 *  listener has already run: `at` read in this handler is the state from BEFORE
 *  that close, so a non-null value means this pointerdown was the dismissal and
 *  there is nothing to do. Keyboard opens it too, since a control reachable by
 *  Tab that only answers a pointer is a control with no keyboard at all.
 *
 *  **Exported** — `HubResults.tsx` reuses it verbatim for the fit-level and
 *  params-band menus that live beside the results table, rather than the
 *  triggers here. Those two only mean anything once a search is ACTIVE
 *  (there is nothing to sift on the idle "your models" face), so they render
 *  in the search face's own component, not this shared header above both
 *  faces (see `HubResults.tsx`'s own doc for the full argument).
 */
export function ControlMenu({
  icon,
  label,
  title,
  ariaLabel,
  items,
}: {
  icon: ReactNode;
  label: string;
  title: string;
  ariaLabel: string;
  items: MenuEntry[];
}) {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  // Anchored to the trigger's rect, not to the pointer: this is a menu ABOUT
  // this button, and ContextMenu clamps it into the viewport from there.
  const openUnder = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    setAt({ x: r.left, y: r.bottom + 4 });
  };
  return (
    <>
      <button
        type="button"
        className={"am-hub-menu" + (at ? " open" : "")}
        aria-haspopup="menu"
        aria-expanded={at !== null}
        aria-label={ariaLabel}
        title={title}
        onPointerDown={(e) => {
          if (at) return; // this pointerdown already closed it — see above
          openUnder(e.currentTarget);
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          // Otherwise the browser turns the key into a click, which lands as a
          // second toggle on the button we just opened from.
          e.preventDefault();
          if (at) {
            setAt(null);
            return;
          }
          openUnder(e.currentTarget);
        }}
      >
        <span className="am-hub-menu-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="am-hub-menu-label">{label}</span>
        <Caret open={at !== null} />
      </button>
      {at && <ContextMenu x={at.x} y={at.y} items={items} onClose={() => setAt(null)} />}
    </>
  );
}

export function SearchControls({
  query,
  task,
  sort,
  includeUnfit,
  showsReset,
  searchBox,
  onQuery,
  onTask,
  onSort,
  onIncludeUnfit,
  onClear,
}: {
  query: string;
  task: string;
  sort: ResultSort;
  /** "Show models that will not fit here" — off by default (see
   *  `HubSearchResult.hiddenUnfit`), and stated as a checkbox rather than
   *  buried in a menu: it is a filter over a fact the page itself computed
   *  (D316's own "never a silent drop"), not one more ordering. */
  includeUnfit: boolean;
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
  onIncludeUnfit: (v: boolean) => void;
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
  const taskItems: MenuEntry[] = [
    {
      label: "Any task",
      icon: MenuIcons.filter,
      active: !task.trim(),
      // "Any task" means any task THIS APP RUNS — the menu below holds only
      // those (D313), and so does an unfiltered search.
      onClick: () => onTask(""),
    },
    ...(tasks.length ? (["separator"] as MenuEntry[]) : []),
    ...tasks.map(
      (t): MenuEntry => ({
        label: t.label,
        icon: MenuIcons.filter,
        active: t.tag === task,
        onClick: () => onTask(t.tag),
      }),
    ),
  ];

  const sortItems: MenuEntry[] = SORTS.map((s) => ({
    label: s.label,
    icon: SORT_ICONS[s.value],
    active: s.value === sort,
    onClick: () => onSort(s.value),
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
        items={taskItems}
      />
      <ControlMenu
        icon={SORT_ICONS[activeS.value]}
        label={activeS.label}
        title={activeS.title}
        ariaLabel={"Sort results: " + activeS.label}
        items={sortItems}
      />
      <label className="am-hub-unfit-toggle" title="Include models this machine likely cannot run">
        <input
          type="checkbox"
          checked={includeUnfit}
          onChange={(e) => onIncludeUnfit(e.target.checked)}
        />
        Show models that will not fit
      </label>
    </div>
  );
}
