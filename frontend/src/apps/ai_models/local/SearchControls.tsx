// The controls row for the full Hub search screen (`HubSearchScreen.tsx`):
// Task / Fit / Size / Sort menus, the two free-text filters (quant,
// publisher), and the result line beneath them.
//
// Ported from the approved mockup's `controls()` — with one deliberate
// narrowing. The mockup's menus render a hover SENTENCE under every option
// (`<p class="h">`); this app's one menu surface, `@platform/ui/ContextMenu`,
// has no slot for that (`MenuEntry` carries a label and an icon, nothing
// else). Reproducing the mockup's `.dd`/`.l`/`.h` markup would mean a second,
// hand-rolled dropdown beside the app's one menu surface — exactly the drift
// `scripts/check-boundaries.mjs` and this file's own history (see the
// now-superseded doc below) exist to prevent. So every menu here keeps
// `ContextMenu`, and the hover sentence rides on the TRIGGER's own `title`
// instead of under each option — one sentence instead of five, but never
// zero.
//
// **The task vocabulary is still the server's, never a literal list**
// (D313, HS-0a): "only the server knows which pipeline tags a registered
// runner can serve", so the mockup's fixed five-entry `MENUS.task` cannot be
// copied in as a constant. `getHubTasks()` is asked once, here, same as
// before.
//
// No query box in this file any more — `HubSearchScreen` owns the one
// `.bigsearch` input the mockup gives the whole screen, above this row.
import { useEffect, useState, type ReactNode } from "react";
import {
  activeFitLevel,
  activeParamsBand,
  activeSort,
  activeTask,
  FIT_LEVELS,
  PARAMS_BANDS,
  SORTS,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import { getHubTasks, type HubFitLevel, type HubParamsBand, type HubTask } from "@platform/lib/api";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";

/** Which glyph each ordering wears — see this module's own history for why
 *  each choice is what it is (`downloads` reuses the download tray, `fit`
 *  the info glyph, `size` the drive glyph). */
const SORT_ICONS: Record<ResultSort, ReactNode> = {
  best: MenuIcons.target,
  downloads: MenuIcons.download,
  likes: MenuIcons.heart,
  updated: MenuIcons.clock,
  created: MenuIcons.sparkle,
  trending: MenuIcons.share,
  fit: MenuIcons.info,
  size: MenuIcons.drive,
};

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

/** One of the row's menus: a bordered trigger showing what is in force, and
 *  `ContextMenu` hanging off its bottom-left corner. Exported so
 *  `HubSearchScreen` can reuse it verbatim rather than a second hand-rolled
 *  trigger — see this file's own doc for why a third menu surface is exactly
 *  the drift to avoid. */
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
          if (at) return; // this pointerdown already closed it — see ControlMenu's own doc
          openUnder(e.currentTarget);
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
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
  task,
  sort,
  fitLevel,
  paramsBand,
  quant,
  publisher,
  includeUnfit,
  onTask,
  onSort,
  onFitLevel,
  onParamsBand,
  onQuant,
  onPublisher,
  onIncludeUnfit,
  loading,
  matchCount,
  hiddenUnfit,
}: {
  task: string;
  sort: ResultSort;
  fitLevel: HubFitLevel;
  paramsBand: HubParamsBand;
  quant: string;
  publisher: string;
  /** "Show models that will not fit here" — off by default (D316's own
   *  "never a silent drop"): the result line beneath states how many are
   *  hidden either way. */
  includeUnfit: boolean;
  onTask: (task: string) => void;
  onSort: (sort: ResultSort) => void;
  onFitLevel: (v: HubFitLevel) => void;
  onParamsBand: (v: HubParamsBand) => void;
  onQuant: (v: string) => void;
  onPublisher: (v: string) => void;
  onIncludeUnfit: (v: boolean) => void;
  /** The result line's own three-way state — loading, a count, or nothing
   *  yet asked. */
  loading: boolean;
  matchCount: number | null;
  hiddenUnfit: number;
}) {
  const [tasks, setTasks] = useState<HubTask[]>([]);

  useEffect(() => {
    // D313: the filter list comes from the server because only the server
    // knows which pipeline tags a registered runner can serve — a hardcoded
    // menu would offer filters for models this app cannot load.
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
  const activeFit = activeFitLevel(fitLevel);
  const activeParams = activeParamsBand(paramsBand);

  const taskItems: MenuEntry[] = [
    {
      label: "Any task",
      icon: MenuIcons.filter,
      active: !task.trim(),
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

  const fitItems: MenuEntry[] = FIT_LEVELS.map((l) => ({
    label: l.label,
    icon: MenuIcons.info,
    active: l.value === fitLevel,
    onClick: () => onFitLevel(l.value),
  }));

  const paramsItems: MenuEntry[] = PARAMS_BANDS.map((b) => ({
    label: b.label,
    icon: MenuIcons.drive,
    active: b.value === paramsBand,
    onClick: () => onParamsBand(b.value),
  }));

  const sortItems: MenuEntry[] = SORTS.map((s) => ({
    label: s.label,
    icon: SORT_ICONS[s.value],
    active: s.value === sort,
    onClick: () => onSort(s.value),
  }));

  return (
    <div data-part="controls">
      <div className="am-hub-controls">
        <ControlMenu
          icon={MenuIcons.filter}
          label={activeT.label}
          title={activeT.title}
          ariaLabel={"Filter by task: " + activeT.label}
          items={taskItems}
        />
        <ControlMenu
          icon={MenuIcons.info}
          label={activeFit.label}
          title={activeFit.title}
          ariaLabel={"Filter by fit: " + activeFit.label}
          items={fitItems}
        />
        <ControlMenu
          icon={MenuIcons.drive}
          label={activeParams.label}
          title={activeParams.title}
          ariaLabel={"Filter by parameter count: " + activeParams.label}
          items={paramsItems}
        />
        <input
          className="am-hub-textfilter"
          type="text"
          value={quant}
          placeholder="Quant (e.g. Q4_K_M)"
          aria-label="Filter by exact quantization"
          title="Show only results with this exact measured quantization"
          onChange={(e) => onQuant(e.target.value)}
        />
        <input
          className="am-hub-textfilter"
          type="text"
          value={publisher}
          placeholder="Publisher/org"
          aria-label="Filter by publisher or organization"
          title="Show only results published by this Hub user or organization"
          onChange={(e) => onPublisher(e.target.value)}
        />
        <span className="am-hub-controls-push" />
        <ControlMenu
          icon={SORT_ICONS[activeS.value]}
          label={activeS.label}
          title={activeS.title}
          ariaLabel={"Sort results: " + activeS.label}
          items={sortItems}
        />
      </div>
      <div className="am-hub-resultline" data-part="resultline">
        <span>
          {loading
            ? "Searching…"
            : matchCount === null
              ? ""
              : matchCount === 0
                ? "0 matches"
                : `${matchCount} match${matchCount === 1 ? "" : "es"}${
                    hiddenUnfit > 0 && !includeUnfit ? ` · ${hiddenUnfit} hidden — will not fit here` : ""
                  }`}
        </span>
        <span className="am-hub-controls-push" />
        <label className="am-hub-unfit-toggle" title="Include models this machine likely cannot run">
          <input
            type="checkbox"
            checked={includeUnfit}
            onChange={(e) => onIncludeUnfit(e.target.checked)}
          />
          Show models that will not fit
        </label>
      </div>
    </div>
  );
}
