// The two dropdowns the explorer's bars are built from.
//
// ModeMenu is the ONE mode control: a bordered trigger showing the active
// mode's icon, the mode's name, and a caret; the dropdown lists every mode
// with radio semantics. It replaces three different surfaces that each said
// "which view is this?" in a different dialect — the topbar's icon strip, the
// preview pane's compact icon strip, and the pane bar's icon-only menu — so
// the same control now appears in all of them, identically.
//
// The icon is drawn in currentColor at the same 16px as every other bar glyph.
// An earlier pass put it in a filled accent chip to mark "this is the active
// mode"; on screen that read as a coloured badge shouting for attention in a
// row of quiet chrome. The active row's own wash in the dropdown carries it
// instead.
//
// OverflowMenu is the `⋮` companion: low-frequency one-shot actions (reveal
// in the file manager, copy path, open in a new tab) that used to be welded
// into the crumb strip as bare glyphs.
//
// BOTH POPUPS ARE PORTALED, and that is a requirement rather than a default:
// `.panel-pane` and the tab bar clip their overflow, so a menu positioned
// inside the bar works in three of four bars and is invisible in the fourth.
// The hand-rolled `position: fixed` anchoring this module used to carry
// (useMenuAnchor — trigger rect, viewport clamping, outside-pointerdown,
// Escape, window blur) is all the shadcn DropdownMenu's job now; base-ui does
// the same three closes, and the window-blur one matters here for the same
// reason it always did (a click landing inside any iframe never reaches this
// document, but it does blur the shell window, and these bars sit above a grid
// of iframes).
//
// `.bar-menu-popup` SURVIVES AS A HOOK on the popup. Breadcrumb's click-away
// listener and its BAR_EDIT_EXCLUDE selector both name it: a press inside a
// bar menu must not be read as "dismiss the path field" or as "open the path
// field". `closest()` walks the portal's own subtree, so the class still
// answers from `<body>`.
import { useEffect, useState, type ReactNode } from "react";
import { ChevronDownIcon } from "lucide-react";
import { modeTitle } from "@platform/lib/mode-name";
import { cn } from "@platform/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import { BarButton } from "@apps/explorer/bar/BarButton";
import { ModeGlyph } from "@apps/explorer/bar/ModeGlyph";

// A click landing inside any iframe never reaches this document, but it does
// blur the shell window — and these bars live above a grid of iframes, so this
// is the common close path, not the exotic one. base-ui closes on outside
// pointerdown and Escape by itself; window blur is the one it cannot see.
function useCloseOnWindowBlur(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    window.addEventListener("blur", close);
    return () => window.removeEventListener("blur", close);
  }, [open, close]);
}

// The rows of a bar dropdown: 16px glyph slot, then the label. Plain glyph, no
// box — the row's own hover/active wash carries the state, and a bordered well
// around every icon made the list read as a grid of buttons rather than a set
// of choices.
const MENU_ITEM = "gap-2.5 px-2 py-1.5 text-xs text-foreground [&_svg]:size-4";

// VERTICAL `⋮`, not the horizontal `···` it was — in every bar that carries this
// menu, because there is one glyph for one meaning. It earns the rotation in the
// place it is most used: the crumb strip, immediately after the path's last
// segment (the panel pane bars, and the shell bar's own path menu before it
// became a right-click), where a horizontal triplet reads as a continuation of
// the path — three more dots in a row of `/`-joined segments, i.e. "the path
// goes on". Turned upright it reads as a control, and it is the same "more,
// about this thing" affordance every file manager puts beside a row.
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
  // Pane scale (the split panel's bars): 24px control, 14px glyph.
  dense?: boolean;
}

export function ModeMenu({ entries, active, busy, onSelect, dense = false }: ModeMenuProps) {
  const [open, setOpen] = useState(false);
  useCloseOnWindowBlur(open, () => setOpen(false));
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
    <DropdownMenu open={open} onOpenChange={setOpen}>
      {/* `mode-menu` stays on the trigger: platform/lib/tours/explorer.ts
          anchors a tour step at `.pane-header .mode-menu`. It carries no
          styling of its own any more. */}
      <DropdownMenuTrigger
        render={<BarButton tone="bordered" dense={dense} className="mode-menu" />}
        aria-label={activeEntry || switching ? "View mode: " + label : label}
        title={switching ? label + " — switching…" : "Change view mode"}
      >
        {/* The spinner takes the icon's place for the length of the switch. */}
        <ModeGlyph dense={dense}>
          {switching ? <span className="mode-icon-spinner" /> : activeEntry?.icon}
        </ModeGlyph>
        {/* The label slot is sized by EVERY mode name at once (hidden ghost
            rows stacked on one grid cell), so the trigger holds the widest
            name's width: switching modes doesn't resize the button, and the
            popup — floored to the trigger — stays the same width too. */}
        <span className="inline-grid text-left font-medium">
          {entries.map((e) => (
            <span key={e.mode} className="col-start-1 row-start-1 invisible whitespace-nowrap" aria-hidden="true">
              {modeTitle(e.mode)}
            </span>
          ))}
          <span className="col-start-1 row-start-1 whitespace-nowrap">{label}</span>
        </span>
        <ChevronDownIcon
          className={cn(
            "shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none",
            dense ? "size-3" : "size-3.5",
            open && "rotate-180",
          )}
        />
      </DropdownMenuTrigger>
      {/* Floored to the trigger's width, not fixed to it: a menu noticeably
          wider than its trigger reads as belonging to something else, and one
          narrower than it reads as a mis-anchored box. */}
      <DropdownMenuContent
        className="bar-menu-popup w-auto min-w-(--anchor-width) rounded-lg p-1"
        role="menu"
        aria-label="View mode"
      >
        {entries.map((e) => {
          const isActive = e.mode === activeEntry?.mode;
          return (
            <DropdownMenuItem
              key={e.mode}
              role="menuitemradio"
              aria-checked={isActive}
              className={cn(
                MENU_ITEM,
                // The active row is the mode you are looking at — semibold, a
                // brighter glyph, and a wash over the whole row (it replaced a
                // trailing checkmark: the row itself carries the state). Kept
                // stronger than the hover so the two never blur.
                isActive && "bg-accent/40 font-semibold focus:bg-accent/60",
              )}
              /* Two ways to be unselectable, one mechanism: the disabled row
                 and its native tooltip. The spinner is the PENDING one's alone
                 — an unavailable row keeps its own icon, because it is not
                 waiting for anything and a spinner over a settled answer reads
                 as a menu that never finishes loading. */
              disabled={e.pending || !!e.disabledReason}
              title={e.pending ? "Checking if this view applies…" : e.disabledReason}
              onClick={() => onSelect(e.mode)}
            >
              <ModeGlyph className={isActive ? "text-foreground" : "text-muted-foreground"}>
                {e.pending ? <span className="mode-icon-spinner" /> : e.icon}
              </ModeGlyph>
              <span className="flex-1">{modeTitle(e.mode)}</span>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export interface OverflowItem {
  label: string;
  onClick: () => void;
  // Optional leading glyph, in the same 16px slot the mode rows use. A menu is
  // all-or-nothing about icons in practice — one iconless row among icon'd ones
  // reads as a broken row — so a caller either gives every item one or none.
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
export function OverflowMenu({
  items,
  title = "More actions",
  dense = false,
}: {
  items: OverflowEntry[];
  title?: string;
  dense?: boolean;
}) {
  const [open, setOpen] = useState(false);
  useCloseOnWindowBlur(open, () => setOpen(false));
  if (items.length === 0) return null;
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      {/* `bar-overflow` stays as a HOOK: Breadcrumb's BAR_EDIT_EXCLUDE and its
          click-away listener both name it, so a press on this trigger neither
          opens nor dismisses the path field. */}
      <DropdownMenuTrigger
        render={<BarButton icon dense={dense} className="bar-overflow" />}
        aria-label={title}
        title={title}
        // Don't take focus on mouse-down. In the crumb bar this menu sits
        // beside a path field that closes on blur: stealing focus would close
        // it, reflow the strip under the pointer, and the mouse-up would land
        // somewhere else. Keyboard focus is unaffected.
        onMouseDown={(e) => e.preventDefault()}
      >
        <EllipsisIcon />
      </DropdownMenuTrigger>
      {/* Right-anchored: a right-zone control wants the two right edges lined
          up. Content width with a small floor, so a one-item menu is still a
          comfortable click target. */}
      <DropdownMenuContent
        className="bar-menu-popup w-auto min-w-30 rounded-lg p-1"
        align="end"
        role="menu"
        aria-label={title}
      >
        {items.map((item, i) =>
          item === "separator" ? (
            // Inset to the rows' own padding so it reads as a break in the list
            // rather than a line drawn across the box.
            <DropdownMenuSeparator key={"sep" + i} className="mx-2 my-1" />
          ) : (
            <DropdownMenuItem key={item.label} className={MENU_ITEM} onClick={item.onClick}>
              {item.icon && <ModeGlyph className="text-muted-foreground">{item.icon}</ModeGlyph>}
              <span className="flex-1">{item.label}</span>
            </DropdownMenuItem>
          ),
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
