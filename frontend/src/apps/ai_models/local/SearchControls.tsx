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
// `.chk` markup verbatim (CSS in ai-models.css, scoped to `.tp`). It owns
// its own outside-click/Escape dismissal, the same contract that shared
// surface gives every other menu on this page.
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
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // Same dismissal contract `ContextMenu` gave every menu on this page:
  // any outside pointerdown or Escape closes it.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        type="button"
        className={"menubtn" + (active ? " active" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => setOpen((o) => !o)}
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
}: {
  keyLabel: string;
  value: string;
  options: HubFacetOption[];
  placeholder: string;
  ariaLabel: string;
  title: string;
  onChange: (v: string) => void;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setFilter("");
    inputRef.current?.focus();
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const narrowed = filter
    ? options.filter((o) => o.id.toLowerCase().includes(filter.toLowerCase()))
    : options;

  const apply = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        type="button"
        className={"menubtn" + (value ? " active" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => setOpen((o) => !o)}
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
        />
        <ControlMenu
          keyLabel="Size:"
          valueLabel={activeParams.label}
          title={activeParams.title}
          ariaLabel={"Filter by parameter count: " + activeParams.label}
          active={paramsBand !== "any"}
          onClear={() => onParamsBand("any")}
          items={paramsItems}
        />
        <SearchMenu
          keyLabel="Quant:"
          value={quant}
          options={facets?.quants ?? []}
          placeholder="Type to filter, e.g. Q4_K_M…"
          ariaLabel="Filter by exact quantization"
          title="Show only results with this exact measured quantization"
          onChange={onQuant}
        />
        <SearchMenu
          keyLabel="Publisher:"
          value={publisher}
          options={facets?.publishers ?? []}
          placeholder="Type to filter…"
          ariaLabel="Filter by publisher or organization"
          title="Show only results published by this Hub user or organization"
          onChange={onPublisher}
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
