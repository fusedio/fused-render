// The controls row for the full Hub search screen (`HubSearchScreen.tsx`):
// Fit / Size / Sort menus, the two free-text filters (quant, publisher), and
// the result line beneath them.
//
// Ported from the approved mockup's `controls()`. Item B (fix round 2):
// the app's one shared dropdown (the platform menu surface) has no slot for
// the mockup's hover SENTENCE under every option (`<p class="h">`) — its
// entries carry a label and an icon, nothing else. Rather than grow that
// shared surface a field only this one screen uses, `ControlMenu` below is
// its own small dropdown, matching the mockup's `.menubtn`/`.dd`/`.l`/`.h`/
// `.chk` markup verbatim (CSS in ai-models.css, scoped to `.tp`).
//
// Item 1 (fix round 7): open/close is now OWNED by `SearchControls`, one
// `openMenu` id for the whole row, not by each `ControlMenu`/`SearchMenu`
// instance — round 6's version gave each menu its own independent `open`
// state, so opening Sort didn't close an already-open Fit/Quant/Publisher
// menu and up to four could be open at once. `SearchControls` registers a
// single document-level `mousedown`+`Escape` listener (only while a menu is
// open) that closes `openMenu` on an outside mousedown or Escape, checking
// against the open menu's own root element (via each menu's `rootRef`
// prop) — the same dismissal contract the app's shared menu surface gives
// every other menu on this page, just centralized to one open slot instead
// of one per menu.
//
// **No Task menu** (D843, round 5): the left pane already scopes this whole
// screen to one capability (`HubSearchScreen`'s own `capabilityKey` prop,
// sent to the server as `capability`), so a second, independent task filter
// inside the search controls had no job left — see D843 for the search-scope
// change this followed from. The server-driven task glossary this file used
// to fetch (D313) and `activeTask` (`hubSearchView.ts`) are unused here now;
// both stay for whatever else still reads them.
//
// No query box in this file any more — `HubSearchScreen` owns the one
// `.bigsearch` input the mockup gives the whole screen, above this row.
import { useEffect, useRef, useState } from "react";
import {
  activeFitLevel,
  activeParamsBand,
  activeSort,
  FIT_LEVELS,
  PARAMS_BANDS,
  SORTS,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import {
  type HubFacetOption,
  type HubFitLevel,
  type HubParamsBand,
  type HubSearchFacets,
} from "@platform/lib/api";

/** One row of an open `ControlMenu` dropdown — the mockup's own shape: a
 *  label, the hover sentence explaining its consequence (`<p class="h">`,
 *  shown for every option, not just on hover), and whether it is the option
 *  currently in force. */
export interface MenuOption {
  label: string;
  hint: string;
  active: boolean;
  onClick: () => void;
}

/** One of the row's menus: a `.menubtn` trigger reading `Key: value`, and
 *  the mockup's own `.dd` dropdown hanging off its bottom-left corner.
 *  `keyLabel` is the muted prefix inside the trigger ("Task:", "Fit:",
 *  "Size:", "Sort:"); `onClear`, when given, draws the `.x` remover for a
 *  non-default selection and resets it without opening the menu. */
export function ControlMenu({
  keyLabel,
  valueLabel,
  title,
  ariaLabel,
  active,
  onClear,
  items,
  align,
  open,
  onOpenChange: setOpen,
  rootRef,
}: {
  keyLabel: string;
  valueLabel: string;
  title: string;
  ariaLabel: string;
  /** Whether a non-default option is in force — draws `.menubtn.active`. */
  active: boolean;
  onClear?: () => void;
  items: MenuOption[];
  /** Item 6 (fix round 6): "right" anchors the `.dd` to its trigger's RIGHT
   *  edge (`.dd.right`) instead of the default left — for a menu whose
   *  trigger sits in the controls row's right half, where a left-anchored
   *  dropdown runs past the scrolling pane's edge and is clipped. */
  align?: "left" | "right";
  /** Item 1 (fix round 7): open state is now owned by `SearchControls` — at
   *  most one dropdown in the row is open at a time, and one shared
   *  document listener (there, not here) closes it on an outside mousedown
   *  or Escape. `rootRef` registers this menu's root element so that shared
   *  listener can tell an inside click from an outside one. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  rootRef: (el: HTMLDivElement | null) => void;
}) {
  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        type="button"
        className={"menubtn" + (active ? " active" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => setOpen(!open)}
      >
        <span className="k">{keyLabel}</span>
        {valueLabel}
        <span className="caret">▾</span>
        {active && onClear && (
          <span
            className="x"
            role="button"
            aria-label={`Clear ${keyLabel.replace(":", "")} filter`}
            onClick={(e) => {
              e.stopPropagation();
              onClear();
              setOpen(false);
            }}
          >
            ×
          </span>
        )}
      </button>
      {open && (
        <div className={"dd" + (align === "right" ? " right" : "")} role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitemradio"
              aria-checked={it.active}
              className={it.active ? "on" : undefined}
              onClick={() => {
                it.onClick();
                setOpen(false);
              }}
            >
              <span className="l">
                <span className="chk">{it.active ? "✓" : ""}</span>
                {it.label}
              </span>
              <p className="h">{it.hint}</p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Item 5 (fix round 6): a searchable dropdown replacing a free-text input
 *  for Publisher/Quant — the trigger reads `Key: value` exactly like
 *  `ControlMenu`, but its `.dd` opens with a filter `<input>` on top (matches
 *  `.am-hub-textfilter`'s own metrics, reused as `.dd .textfilter`) that
 *  narrows `options` by a case-insensitive substring match, plus a muted
 *  count next to each. Typing a value that never appears in the list and
 *  pressing Enter still applies it as free text — the Hub has far more
 *  publishers/quants than any one search's facets will enumerate, and this
 *  keeps that reachable without a second, separate input. */
function SearchMenu({
  keyLabel,
  value,
  options,
  placeholder,
  ariaLabel,
  title,
  onChange,
  align,
  open,
  onOpenChange: setOpen,
  rootRef,
}: {
  keyLabel: string;
  value: string;
  options: HubFacetOption[];
  placeholder: string;
  ariaLabel: string;
  title: string;
  onChange: (v: string) => void;
  align?: "left" | "right";
  /** Item 1 (fix round 7): see `ControlMenu`'s matching props — open state
   *  and outside-click/Escape dismissal moved up to `SearchControls`, one
   *  `openMenu` for the whole row. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  rootRef: (el: HTMLDivElement | null) => void;
}) {
  const [filter, setFilter] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setFilter("");
    inputRef.current?.focus();
  }, [open]);

  const narrowed = filter
    ? options.filter((o) => o.id.toLowerCase().includes(filter.toLowerCase()))
    : options;

  const apply = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  // Item 1 (fix round 11): mlx-community has exactly one row in the ~200
  // most-downloaded window `_facets` (hub_models.py) counts over, so it
  // sorts behind the top-40 cutoff and never appears here — the dropdown
  // then shows nothing for a value that DOES exist on the Hub (Enter still
  // sends it and gets a full page back). Whenever the typed text has no
  // exact case-insensitive match among `options`, offer it as a search: one
  // extra row at the top doing exactly what Enter does, reusing the same
  // option-row markup so it reads as just another row, not a new affordance.
  const trimmed = filter.trim();
  const hasExactMatch = options.some(
    (o) => o.id.toLowerCase() === trimmed.toLowerCase(),
  );
  const showSearchRow = trimmed !== "" && !hasExactMatch;
  const searchKind = keyLabel.replace(":", "").trim().toLowerCase();

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        type="button"
        className={"menubtn" + (value ? " active" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => setOpen(!open)}
      >
        <span className="k">{keyLabel}</span>
        {value || "Any"}
        <span className="caret">▾</span>
        {value && (
          <span
            className="x"
            role="button"
            aria-label={`Clear ${keyLabel.replace(":", "")} filter`}
            onClick={(e) => {
              e.stopPropagation();
              apply("");
            }}
          >
            ×
          </span>
        )}
      </button>
      {open && (
        <div className={"dd" + (align === "right" ? " right" : "")} role="menu">
          <input
            ref={inputRef}
            className="textfilter"
            type="text"
            value={filter}
            placeholder={placeholder}
            aria-label={ariaLabel}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && filter.trim()) {
                e.preventDefault();
                apply(filter.trim());
              }
            }}
          />
          {showSearchRow && (
            <button
              key="__search__"
              type="button"
              role="menuitemradio"
              aria-checked={false}
              onClick={() => apply(trimmed)}
            >
              <span className="l">
                <span className="chk"></span>
                {`Search ${searchKind} "${trimmed}"`}
              </span>
            </button>
          )}
          {narrowed.map((o) => (
            <button
              key={o.id}
              type="button"
              role="menuitemradio"
              aria-checked={o.id === value}
              className={o.id === value ? "on" : undefined}
              onClick={() => apply(o.id)}
            >
              <span className="l">
                <span className="chk">{o.id === value ? "✓" : ""}</span>
                {o.id}
                <span className="count">{o.count}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function SearchControls({
  sort,
  fitLevel,
  paramsBand,
  quant,
  publisher,
  onSort,
  onFitLevel,
  onParamsBand,
  onQuant,
  onPublisher,
  loading,
  matchCount,
  facets,
}: {
  sort: ResultSort;
  fitLevel: HubFitLevel;
  paramsBand: HubParamsBand;
  quant: string;
  publisher: string;
  onSort: (sort: ResultSort) => void;
  onFitLevel: (v: HubFitLevel) => void;
  onParamsBand: (v: HubParamsBand) => void;
  onQuant: (v: string) => void;
  onPublisher: (v: string) => void;
  /** The result line's own three-way state — loading, a count, or nothing
   *  yet asked. */
  loading: boolean;
  matchCount: number | null;
  /** Item 5 (fix round 6): the Publisher/Quant menus' option lists, computed
   *  server-side pre-narrowing. `null` before the first search response. */
  facets?: HubSearchFacets | null;
}) {
  const activeS = activeSort(sort);
  const activeFit = activeFitLevel(fitLevel);
  const activeParams = activeParamsBand(paramsBand);

  // Item 1 (fix round 7): a single `openMenu` for the whole row — opening
  // one dropdown closes any other that was open, instead of each menu
  // owning its own independent `open` state (the round-6 bug: Fit, Sort,
  // Quant and Fit could all be open at once). One shared document-level
  // `mousedown`/`Escape` listener, registered only while a menu is open,
  // closes the open menu when the event lands outside ITS root — `roots`
  // holds each menu's root element, keyed by the same id used for
  // `openMenu`, filled in by the `rootRef` callback each menu is given.
  type MenuId = "fit" | "params" | "quant" | "publisher" | "sort";
  const [openMenu, setOpenMenu] = useState<MenuId | null>(null);
  const roots = useRef<Partial<Record<MenuId, HTMLDivElement | null>>>({});
  const setRoot = (id: MenuId) => (el: HTMLDivElement | null) => {
    roots.current[id] = el;
  };

  useEffect(() => {
    if (!openMenu) return;
    const onMouseDown = (e: MouseEvent) => {
      const root = roots.current[openMenu];
      if (root && !root.contains(e.target as Node)) setOpenMenu(null);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenMenu(null);
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [openMenu]);

  const fitItems: MenuOption[] = FIT_LEVELS.map((l) => ({
    label: l.label,
    hint: l.title,
    active: l.value === fitLevel,
    onClick: () => onFitLevel(l.value),
  }));

  const paramsItems: MenuOption[] = PARAMS_BANDS.map((b) => ({
    label: b.label,
    hint: b.title,
    active: b.value === paramsBand,
    onClick: () => onParamsBand(b.value),
  }));

  const sortItems: MenuOption[] = SORTS.map((s) => ({
    label: s.label,
    hint: s.title,
    active: s.value === sort,
    onClick: () => onSort(s.value),
  }));

  return (
    <div data-part="controls">
      <div className="am-hub-controls">
        <ControlMenu
          keyLabel="Fit:"
          valueLabel={activeFit.label}
          title={activeFit.title}
          ariaLabel={"Filter by fit: " + activeFit.label}
          active={fitLevel !== "any"}
          onClear={() => onFitLevel("any")}
          items={fitItems}
          open={openMenu === "fit"}
          onOpenChange={(v) => setOpenMenu(v ? "fit" : null)}
          rootRef={setRoot("fit")}
        />
        <ControlMenu
          keyLabel="Size:"
          valueLabel={activeParams.label}
          title={activeParams.title}
          ariaLabel={"Filter by parameter count: " + activeParams.label}
          active={paramsBand !== "any"}
          onClear={() => onParamsBand("any")}
          items={paramsItems}
          open={openMenu === "params"}
          onOpenChange={(v) => setOpenMenu(v ? "params" : null)}
          rootRef={setRoot("params")}
        />
        <SearchMenu
          keyLabel="Quant:"
          value={quant}
          options={facets?.quants ?? []}
          placeholder="Type to filter, e.g. Q4_K_M…"
          ariaLabel="Filter by exact quantization"
          title="Show only results with this exact measured quantization"
          onChange={onQuant}
          open={openMenu === "quant"}
          onOpenChange={(v) => setOpenMenu(v ? "quant" : null)}
          rootRef={setRoot("quant")}
        />
        <SearchMenu
          keyLabel="Publisher:"
          value={publisher}
          options={facets?.publishers ?? []}
          placeholder="Type to filter…"
          ariaLabel="Filter by publisher or organization"
          title="Show only results published by this Hub user or organization"
          onChange={onPublisher}
          open={openMenu === "publisher"}
          onOpenChange={(v) => setOpenMenu(v ? "publisher" : null)}
          rootRef={setRoot("publisher")}
        />
        <span className="am-hub-controls-push" />
        <ControlMenu
          keyLabel="Sort:"
          valueLabel={activeS.label}
          title={activeS.title}
          ariaLabel={"Sort results: " + activeS.label}
          active={false}
          items={sortItems}
          align="right"
          open={openMenu === "sort"}
          onOpenChange={(v) => setOpenMenu(v ? "sort" : null)}
          rootRef={setRoot("sort")}
        />
      </div>
      {/* Item 6 (fix round 3): also wears the mockup's own `.resultline`
       *  class — a live check for that exact selector found nothing, since
       *  this row only ever carried `am-hub-controls`'s own naming. */}
      {/* Item 7 (fix round 5): the "Show models that will not fit" toggle
       *  and the "N hidden" count are gone — every model is always shown,
       *  and the per-row red "Will not fit" line (HubSearchScreen.tsx) is
       *  the only warning left, so the result line states only the count. */}
      <div className="am-hub-resultline resultline" data-part="resultline">
        <span>
          {loading ? (
            "Searching…"
          ) : matchCount === null ? (
            ""
          ) : matchCount === 0 ? (
            "0 matches"
          ) : (
            <>
              <b>{matchCount}</b> match{matchCount === 1 ? "" : "es"}
            </>
          )}
        </span>
      </div>
    </div>
  );
}
