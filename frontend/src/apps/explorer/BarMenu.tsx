// The two dropdowns the explorer's bars are built from.
//
// ModeMenu is the ONE mode control: a bordered trigger showing the active
// mode's icon, the mode's name, and a caret; the dropdown lists every mode
// with radio semantics. It replaces three different surfaces that each said
// "which view is this?" in a different dialect — the topbar's icon strip, the
// preview pane's compact icon strip, and the pane bar's icon-only menu — so
// the same control now appears in all of them, identically.
//
// OverflowMenu is the `⋮` companion: low-frequency one-shot actions (reveal
// in the file manager, copy path, open in a new tab) that used to be welded
// into the crumb strip as bare glyphs.
//
// Both menus portal to the body (base-ui), which is what lets them escape the
// overflow-clipping .panel-pane and tab bars they are triggered from.
import { useEffect, useState, type ReactNode } from "react";
import { ChevronDownIcon, EllipsisVerticalIcon } from "lucide-react";
import { modeTitle } from "@platform/lib/mode-name";
import { Button } from "@platform/shadcn/ui/button";
import { Spinner } from "@platform/shadcn/ui/spinner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";

// Close the menu when the shell window blurs: a click landing inside any
// iframe never reaches this document, so base-ui's outside-press detection
// cannot see it — but it does blur the shell window (the pane bars live above
// a grid of iframes, so this is the common case, not the exotic one).
function useCloseOnWindowBlur(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    window.addEventListener("blur", close);
    return () => window.removeEventListener("blur", close);
  }, [open, close]);
}

// Exported so the folder listing's header `⋮` (Listing.tsx) is the SAME glyph
// as the bars' — it opens a menu of the same actions, and a second hand-rolled
// triplet would drift.
export function EllipsisIcon() {
  return <EllipsisVerticalIcon aria-hidden="true" />;
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
  // unavailable; the reasons themselves are canned in lib/mode-visibility.
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
  const [open, setOpen] = useState(false);
  useCloseOnWindowBlur(open, () => setOpen(false));
  const activeEntry = entries.find((e) => e.mode === active) ?? null;
  // One ROW is not a choice — the same rule the icon strips used — unless
  // nothing is active (a caller whose surface can show no mode at all, e.g. the
  // listing pane's self target), where that one entry is the only way to pick
  // anything and the trigger is what offers it.
  //
  // Counted over the ROWS, disabled ones included, and that is the rule rather
  // than an oversight. A companion surface passes its whole closed list — some
  // rows disabled placeholders explaining themselves (see `disabledReason`) —
  // and the menu renders, because those rows are the answer to "why is there
  // only one thing here?". Hiding a menu whose only ENABLED row is the active
  // one is what made a file outside a git repository open with no switcher.
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
      <DropdownMenuTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            aria-label={activeEntry || switching ? "View mode: " + label : label}
            title={switching ? label + " — switching…" : "Change view mode"}
          />
        }
      >
        {/* The spinner takes the icon's place for the length of the switch. */}
        <span className="flex size-4 items-center justify-center">
          {switching ? <Spinner /> : activeEntry?.icon}
        </span>
        {/* The label slot is sized by EVERY mode name at once (hidden ghost
            rows stacked on one grid cell), so the trigger holds the widest
            name's width: switching modes doesn't resize the button, and the
            popup — floored to the trigger — stays the same width too. */}
        <span className="inline-grid text-left">
          {entries.map((e) => (
            <span
              key={e.mode}
              className="invisible col-start-1 row-start-1"
              aria-hidden="true"
            >
              {modeTitle(e.mode)}
            </span>
          ))}
          <span className="col-start-1 row-start-1">{label}</span>
        </span>
        <ChevronDownIcon className="size-3" />
      </DropdownMenuTrigger>
      <DropdownMenuContent aria-label="View mode" className="w-auto min-w-(--anchor-width)">
        <DropdownMenuRadioGroup
          value={activeEntry?.mode}
          onValueChange={(value) => onSelect(String(value))}
        >
          {entries.map((e) => (
            <DropdownMenuRadioItem
              key={e.mode}
              value={e.mode}
              closeOnClick
              /* Two ways to be unselectable, one mechanism: the disabled row
                 and its native tooltip. The spinner is the PENDING one's alone
                 — an unavailable row keeps its own icon, because it is not
                 waiting for anything and a spinner over a settled answer reads
                 as a menu that never finishes loading. */
              disabled={e.pending || !!e.disabledReason}
              title={e.pending ? "Checking if this view applies…" : e.disabledReason}
            >
              <span className="flex size-4 items-center justify-center">
                {e.pending ? <Spinner /> : e.icon}
              </span>
              {modeTitle(e.mode)}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export interface OverflowItem {
  label: string;
  onClick: () => void;
  // Optional leading glyph. A menu is all-or-nothing about icons in practice —
  // one iconless row among icon'd ones reads as a broken row — so a caller
  // either gives every item one or none.
  icon?: ReactNode;
}

// A menu may group its items. Same shape as ContextMenu's entry list, so the
// two menus describe a separator the same way.
export type OverflowEntry = OverflowItem | "separator";

// `⋮` menu for the bars. Renders nothing when it has no items, so a caller can
// pass a conditional list without guarding the control itself.
export function OverflowMenu({ items, title = "More actions" }: { items: OverflowEntry[]; title?: string }) {
  const [open, setOpen] = useState(false);
  useCloseOnWindowBlur(open, () => setOpen(false));
  if (items.length === 0) return null;
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={title}
            title={title}
            // Don't take focus on mouse-down. In the crumb bar this menu sits
            // beside a path field that closes on blur: stealing focus would
            // close it, reflow the strip under the pointer, and the mouse-up
            // would land somewhere else — the button would be unclickable for
            // as long as the field is open. Keyboard focus is unaffected.
            onMouseDown={(e) => e.preventDefault()}
          />
        }
      >
        <EllipsisIcon />
      </DropdownMenuTrigger>
      <DropdownMenuContent aria-label={title} align="end" className="w-auto">
        {items.map((item, i) =>
          item === "separator" ? (
            <DropdownMenuSeparator key={"sep" + i} />
          ) : (
            <DropdownMenuItem key={item.label} onClick={item.onClick}>
              {item.icon && (
                <span className="flex size-4 items-center justify-center">{item.icon}</span>
              )}
              {item.label}
            </DropdownMenuItem>
          )
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
