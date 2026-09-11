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
import { type HubFitLevel, type HubParamsBand } from "@platform/lib/api";

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
}: {
  keyLabel: string;
  valueLabel: string;
  title: string;
  ariaLabel: string;
  /** Whether a non-default option is in force — draws `.menubtn.active`. */
  active: boolean;
  onClear?: () => void;
  items: MenuOption[];
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
        <div className="dd" role="menu">
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
          keyLabel="Sort:"
          valueLabel={activeS.label}
          title={activeS.title}
          ariaLabel={"Sort results: " + activeS.label}
          active={false}
          items={sortItems}
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
