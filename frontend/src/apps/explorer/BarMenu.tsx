// The two dropdowns the explorer's bars are built from, and the anchoring they
// share.
//
// ModeMenu is the ONE mode control: a bordered trigger showing the active
// mode's icon, the mode's name, and a caret; the dropdown lists every mode
// with radio semantics. It replaces three different surfaces that each said
// "which view is this?" in a different dialect — the topbar's icon strip, the
// preview pane's compact icon strip, and the pane bar's icon-only menu — so
// the same control now appears in all of them, identically.
//
// The icon is drawn in currentColor at the same 16px as every other .bar-ctl
// glyph. An earlier pass put it in a filled accent chip to mark "this is the
// active mode"; on screen that read as a coloured badge shouting for
// attention in a row of quiet chrome. The accent survives where it costs
// nothing — the checkmark on the dropdown's active row.
//
// OverflowMenu is the `···` companion: low-frequency one-shot actions (reveal
// in the file manager, copy path, open in a new tab) that used to be welded
// into the crumb strip as bare glyphs.
//
// Both popups are position:fixed off the trigger's rect rather than absolutely
// positioned: .panel-pane and the tab bar clip their overflow, and a menu that
// works in three of four bars is a menu that will be reported as broken in the
// fourth.
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { modeTitle } from "@platform/lib/mode-name";

// Keep the popup on screen: it is right-aligned to the trigger in spirit, but
// clamping the LEFT edge is what actually matters near the window's edge.
const MENU_MIN_W = 180;

// Exactly one of left/right is set: a left-anchored popup grows rightwards from
// the trigger's left edge; a right-anchored one hangs from its right edge so
// the two right edges line up — what a right-zone control wants. The mode
// dropdown sits mid-bar and is fine growing rightwards.
interface MenuPos {
  top: number;
  left?: number;
  right?: number;
}

// Open/close plumbing shared by both menus. Closes on outside pointerdown, on
// Escape, and on window blur — a click landing inside any iframe never reaches
// this document, but it does blur the shell window (the pane bars live above a
// grid of iframes, so this is the common case, not the exotic one).
function useMenuAnchor(align: "left" | "right" = "left") {
  const [pos, setPos] = useState<MenuPos | null>(null); // non-null = open
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!pos) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPos(null);
    };
    const onBlur = () => setPos(null);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, [pos]);

  const toggle = (e: MouseEvent) => {
    // The pane bar's trigger sits inside click-handling chrome (a tab button,
    // a bar that also owns click-to-edit), so the open click is ours alone.
    e.stopPropagation();
    if (pos) {
      setPos(null);
      return;
    }
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    // A right-anchored popup is content-width (see .bar-overflow in
    // explorer.css), so there is no width to subtract from a left coordinate —
    // pin the right edges together and let the box grow leftwards, clamped to
    // the viewport.
    setPos(
      align === "right"
        ? { top: r.bottom + 4, right: Math.max(4, window.innerWidth - r.right) }
        : {
            top: r.bottom + 4,
            left: Math.max(4, Math.min(r.left, window.innerWidth - MENU_MIN_W - 4)),
          }
    );
  };

  return { pos, rootRef, toggle, close: () => setPos(null) };
}

function CaretIcon({ open }: { open: boolean }) {
  return (
    <svg
      className="bar-caret"
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points={open ? "18 15 12 9 6 15" : "6 9 12 15 18 9"} />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      className="bar-menu-check"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function EllipsisIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.7" />
      <circle cx="12" cy="12" r="1.7" />
      <circle cx="19" cy="12" r="1.7" />
    </svg>
  );
}

export interface ModeMenuEntry {
  mode: string;
  icon: ReactNode;
  // Condition.py gate not yet resolved (CT-12): listed as a disabled spinner
  // row until the background /api/fs/conditions verdict lands.
  pending?: boolean;
}

interface ModeMenuProps {
  entries: ModeMenuEntry[];
  active: string;
  // The mode a click is currently switching TO, if any. The switch is async
  // (the preview asks the open editor to flush its buffer first) and clicks
  // landing mid-switch are dropped, so the trigger shows a spinner in place of
  // the mode icon until the swap starts — otherwise a slow switch reads as a
  // dead control.
  busy?: string | null;
  onSelect: (mode: string) => void;
}

export function ModeMenu({ entries, active, busy, onSelect }: ModeMenuProps) {
  const { pos, rootRef, toggle, close } = useMenuAnchor();
  // One mode is not a choice — the same rule the icon strips used.
  if (entries.length <= 1) return null;

  const activeEntry = entries.find((e) => e.mode === active) ?? entries[0];
  const switching = busy !== null && busy !== undefined;
  const label = modeTitle(switching ? (busy as string) : activeEntry.mode);

  return (
    <div className="mode-menu" ref={rootRef}>
      <button
        type="button"
        className="bar-ctl bar-ctl-bordered mode-menu-btn"
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        aria-label={"View mode: " + label}
        title={switching ? label + " — switching…" : "Change view mode"}
        onClick={toggle}
      >
        {/* The spinner takes the icon's place for the length of the switch. */}
        <span className="mode-menu-icon">
          {switching ? <span className="mode-icon-spinner" /> : activeEntry.icon}
        </span>
        <span className="mode-menu-label">{label}</span>
        <CaretIcon open={pos !== null} />
      </button>
      {pos && (
        <div
          className="bar-menu-popup"
          role="menu"
          aria-label="View mode"
          style={{ top: pos.top, left: pos.left, right: pos.right }}
        >
          {entries.map((e) => (
            <button
              key={e.mode}
              type="button"
              role="menuitemradio"
              aria-checked={e.mode === activeEntry.mode}
              className={
                "bar-menu-item" +
                (e.mode === activeEntry.mode ? " active" : "") +
                (e.pending ? " pending" : "")
              }
              disabled={e.pending}
              title={e.pending ? "Checking if this view applies…" : undefined}
              onClick={() => {
                close();
                onSelect(e.mode);
              }}
            >
              <span className="bar-menu-item-icon">
                {e.pending ? <span className="mode-icon-spinner" /> : e.icon}
              </span>
              <span className="bar-menu-item-label">{modeTitle(e.mode)}</span>
              {e.mode === activeEntry.mode && <CheckIcon />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export interface OverflowItem {
  label: string;
  onClick: () => void;
}

// `···` menu for the bars' layout zone. Renders nothing when it has no items,
// so a caller can pass a conditional list without guarding the control itself.
export function OverflowMenu({ items, title = "More actions" }: { items: OverflowItem[]; title?: string }) {
  const { pos, rootRef, toggle, close } = useMenuAnchor("right");
  if (items.length === 0) return null;
  return (
    <div className="bar-overflow" ref={rootRef}>
      <button
        type="button"
        className="bar-ctl bar-ctl-icon"
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        aria-label={title}
        title={title}
        onClick={toggle}
      >
        <EllipsisIcon />
      </button>
      {pos && (
        <div
          className="bar-menu-popup"
          role="menu"
          aria-label={title}
          style={{ top: pos.top, left: pos.left, right: pos.right }}
        >
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className="bar-menu-item"
              onClick={() => {
                close();
                item.onClick();
              }}
            >
              <span className="bar-menu-item-label">{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
