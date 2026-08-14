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
// nothing — the wash on the dropdown's active row (.bar-menu-item.active).
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

// Exactly one of left/right is set: a left-anchored popup grows rightwards from
// the trigger's left edge; a right-anchored one hangs from its right edge so
// the two right edges line up — what a right-zone control wants. The mode
// dropdown sits mid-bar and is fine growing rightwards.
interface MenuPos {
  top: number;
  left?: number;
  right?: number;
  // The trigger's own width, so the popup can floor itself to it (a menu
  // noticeably wider than its trigger reads as belonging to something else).
  width: number;
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
        ? { top: r.bottom + 4, right: Math.max(4, window.innerWidth - r.right), width: r.width }
        : {
            top: r.bottom + 4,
            left: Math.max(4, Math.min(r.left, window.innerWidth - r.width - 4)),
            width: r.width,
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

// VERTICAL `⋮`, not the horizontal `···` it was — in every bar that carries this
// menu, because there is one glyph for one meaning. It earns the rotation in the
// place it is most used: the crumb strip, immediately after the path's last
// segment (the panel pane bars, and the shell bar's own path menu before it
// became a right-click), where a horizontal triplet reads as a continuation of
// the path — three more
// dots in a row of `/`-joined segments, i.e. "the path goes on". Turned upright
// it reads as a control, and it is the same "more, about this thing" affordance
// every file manager puts beside a row.
// Exported so the folder listing's header `⋮` (Listing.tsx) is the SAME glyph
// as the bars' — it opens a menu of the same actions, and a second hand-rolled
// triplet would drift.
export function EllipsisIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <circle cx="12" cy="5" r="1.7" />
      <circle cx="12" cy="12" r="1.7" />
      <circle cx="12" cy="19" r="1.7" />
    </svg>
  );
}

export interface ModeMenuEntry {
  mode: string;
  icon: ReactNode;
  // Condition.py gate not yet resolved (CT-12): listed as a disabled spinner
  // row until the background /api/fs/conditions verdict lands.
  pending?: boolean;
  // Listed, disabled, and SAYING WHY — the row keeps its icon and its name and
  // hands this string to the native tooltip. Used by the two COMPANION surfaces
  // (the file preview's sidebar, the folder listing's pane), whose mode list is
  // a closed set the user is entitled to see all of even where a member is
  // unavailable; the reasons themselves are canned in lib/mode-visibility, which
  // is also where the argument for showing them is written down.
  //
  // Not a second spelling of `pending`: pending says "we don't know yet" and
  // spins, this says "we know, and here is the answer".
  disabledReason?: string;
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
  const activeEntry = entries.find((e) => e.mode === active) ?? null;
  // One ROW is not a choice — the same rule the icon strips used — unless
  // nothing is active (a caller whose surface can show no mode at all, e.g. the
  // listing pane's self target), where that one entry is the only way to pick
  // anything and the trigger is what offers it.
  //
  // Counted over the ROWS, disabled ones included, and that is the rule rather
  // than an oversight. A companion surface passes its whole closed list — three
  // rows over every file, some of them disabled placeholders explaining
  // themselves (see `disabledReason`) — and the menu renders, because those rows
  // are the answer to "why is there only one thing here?". Hiding a menu whose
  // only ENABLED row is the active one is what made a file outside a git
  // repository open with no switcher at all.
  if (!entries.length || (entries.length === 1 && activeEntry)) return null;

  const switching = busy !== null && busy !== undefined;
  // With no active entry the trigger names the ACTION rather than a mode: there
  // is no "current view" to report, and naming one would mis-report it.
  const label = switching
    ? modeTitle(busy as string)
    : activeEntry
      ? modeTitle(activeEntry.mode)
      : "Choose view";

  return (
    <div className="mode-menu" ref={rootRef}>
      <button
        type="button"
        className="bar-ctl bar-ctl-bordered mode-menu-btn"
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        aria-label={activeEntry || switching ? "View mode: " + label : label}
        title={switching ? label + " — switching…" : "Change view mode"}
        onClick={toggle}
      >
        {/* The spinner takes the icon's place for the length of the switch. */}
        <span className="mode-menu-icon">
          {switching ? <span className="mode-icon-spinner" /> : activeEntry?.icon}
        </span>
        {/* The label slot is sized by EVERY mode name at once (hidden ghost
            rows stacked on one grid cell), so the trigger holds the widest
            name's width: switching modes doesn't resize the button, and the
            popup — floored to the trigger — stays the same width too. */}
        <span className="mode-menu-label">
          {entries.map((e) => (
            <span key={e.mode} className="mode-menu-label-ghost" aria-hidden="true">
              {modeTitle(e.mode)}
            </span>
          ))}
          <span>{label}</span>
        </span>
        <CaretIcon open={pos !== null} />
      </button>
      {pos && (
        <div
          className="bar-menu-popup"
          role="menu"
          aria-label="View mode"
          style={{ top: pos.top, left: pos.left, right: pos.right, minWidth: pos.width }}
        >
          {entries.map((e) => (
            <button
              key={e.mode}
              type="button"
              role="menuitemradio"
              aria-checked={e.mode === activeEntry?.mode}
              className={
                "bar-menu-item" +
                (e.mode === activeEntry?.mode ? " active" : "") +
                (e.pending ? " pending" : "")
              }
              /* Two ways to be unselectable, one mechanism: the native disabled
                 button and its native tooltip (`.bar-menu-item:disabled` in
                 explorer.css dims both alike). The spinner is the PENDING one's
                 alone — an unavailable row keeps its own icon, because it is not
                 waiting for anything and a spinner over a settled answer reads
                 as a menu that never finishes loading. */
              disabled={e.pending || !!e.disabledReason}
              title={e.pending ? "Checking if this view applies…" : e.disabledReason}
              onClick={() => {
                close();
                onSelect(e.mode);
              }}
            >
              <span className="bar-menu-item-icon">
                {e.pending ? <span className="mode-icon-spinner" /> : e.icon}
              </span>
              <span className="bar-menu-item-label">{modeTitle(e.mode)}</span>
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
  // Optional leading glyph, in the same 16px slot the mode rows use
  // (.bar-menu-item-icon). A menu is all-or-nothing about icons in practice —
  // one iconless row among icon'd ones reads as a broken row — so a caller
  // either gives every item one or none.
  icon?: ReactNode;
}

// A menu may group its items. Same shape as ContextMenu's entry list, so the
// two menus describe a separator the same way.
export type OverflowEntry = OverflowItem | "separator";

// THE PATH `···`/`⋮` IS GONE from this module. It held the two low-frequency
// one-shots every view OF A PATH offers (reveal, copy path) plus — over a file —
// the two splits, and it had two homes: the crumb bar for a file/preview and the
// listing's own search row for a folder.
//
// Both callers took the items somewhere better. The folder's are in the listing
// header's `⋮` (Listing.tsx), beside the rest of the folder's operations. The
// file's are in the CRUMB BAR'S RIGHT-CLICK MENU (Breadcrumb's onBarContextMenu,
// items from lib/bar-menus), which is where the hand goes first on a bar and
// where they cost no chrome at all — and which is also how Rename and "Open in
// Claude Code", both missing from the four-item dropdown, joined them.
//
// `OverflowMenu` below stays: the panel pane bars still use it for their own
// one-shot ("Open in a new tab").

// `⋮` menu for the bars. Renders nothing when it has no items, so a caller can
// pass a conditional list without guarding the control itself.
export function OverflowMenu({ items, title = "More actions" }: { items: OverflowEntry[]; title?: string }) {
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
        // Don't take focus on mouse-down. In the crumb bar this menu sits
        // beside a path field that closes on blur: stealing focus would close
        // it, reflow the strip under the pointer, and the mouse-up would land
        // somewhere else — the button would be unclickable for as long as the
        // field is open. Keyboard focus is unaffected.
        onMouseDown={(e) => e.preventDefault()}
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
          {items.map((item, i) =>
            item === "separator" ? (
              <div key={"sep" + i} className="bar-menu-sep" role="separator" />
            ) : (
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
                {item.icon && <span className="bar-menu-item-icon">{item.icon}</span>}
                <span className="bar-menu-item-label">{item.label}</span>
              </button>
            )
          )}
        </div>
      )}
    </div>
  );
}
