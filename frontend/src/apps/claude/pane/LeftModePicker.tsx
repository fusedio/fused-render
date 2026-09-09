// WHICH VIEW THE LEFT PANE FRAMES — the `leftmode` picker (T:5474-5667).
//
// A trigger and its own listbox popup, quiet to the same degree as the annotate
// switch: borderless, transparent, 12px, muted until hovered. Loud chrome here
// would read as the pane's TITLE rather than as a switch on it, and the pane's
// title is the file name in the topbar.
//
// It WAS a native `<select>`, chosen for the platform popup and the accessible
// name that brings for free. What overturned that is the ICON: an `<option>`
// renders text and nothing else in every engine, so a mode list that shows what
// the shell's own mode menu shows — the template's icon.svg beside its name —
// cannot be built out of one (T:5484-5500). shadcn's `DropdownMenu` (Base UI
// `Menu`) is the native equivalent: real buttons, so focus, Enter, Space, the
// arrow keys, Escape and the outside-click close are all the platform's job
// rather than this file's (design.md §5 maps `#leftmodepop` → DropdownMenu).
//
// THE PICKER WRITES THE PARAM AND NOTHING ELSE (T:5576-5580). `useFramedSrc` in
// AppPane, driven from the same param, is the only thing that touches the
// iframe — one code path for a user click, for a reload with `leftmode` already
// set, and for any later param change.
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import type { TemplateEntry } from "@platform/lib/api";
import type { ParamsStore } from "../params/store";
import { curLeftEntry, paneModeIconUrl, paneModeLabel, paneModeLetter } from "./paneUrl";

/**
 * WHERE the picker lives, which is a layout question, not a state one
 * (T:5612-5660).
 *
 *   `"leftbar"`  — the SPLIT layout: a row across the top of the left column.
 *                  The control that chooses what that pane frames, sitting on
 *                  that pane. It spent a while in the shared control strip,
 *                  across the divider, which put a control for one column on the
 *                  other one and left the pane it governs with no chrome at all.
 *   `"anntools"` — the NARROW layout: back into the shared strip. There is no
 *                  persistent left column below the breakpoint (one view shows
 *                  at a time), so there is nothing to hang a bar on, and the
 *                  strip is the one row BOTH views keep on screen. It belongs
 *                  BETWEEN the annotate switch and the view toggle — the toggle
 *                  must stay the row's last word, since it is the way out of the
 *                  preview.
 *   `"none"`     — nothing to pick, or no pane. An APP FOLDER has exactly one
 *                  thing to frame (the entry resolved by app.py, which is not a
 *                  stat entry), an ordinary folder has no pane, and a file with a
 *                  single offerable view has no choice either. A one-item picker
 *                  is chrome that cannot do anything (T:5560-5562).
 *
 * T moved ONE element between the two hosts rather than shipping two copies,
 * because a second control would be a second value and a second listener. Here
 * the value lives in the param and exactly one host renders at a time, so the
 * hazard the move existed to avoid does not arise — but the RULE is the same and
 * lives in one place, which is this function.
 */
export function pickerHost(narrow: boolean, noPane: boolean, modeCount: number): "leftbar" | "anntools" | "none" {
  if (noPane || modeCount < 2) return "none";
  return narrow ? "anntools" : "leftbar";
}

/** Whether the pane's own bar exists at all. An empty bordered strip above the
 *  preview would be chrome for a choice that does not exist — the bar follows
 *  the picker (T:5643). Showing or hiding it changes the frame's box, which is
 *  why T re-measures the annotation pins twice around it (T:5661-5667); PR3
 *  wires that through `AppPane`'s `onRemeasure`. */
export function leftBarShown(narrow: boolean, noPane: boolean, modeCount: number): boolean {
  return pickerHost(narrow, noPane, modeCount) === "leftbar";
}

export interface LeftModePickerProps {
  /** The offerable entries, in stat's own order (`PaneDecision.leftModes`). */
  modes: readonly TemplateEntry[];
  /** The chat's param store — the picker's only write target. */
  params: ParamsStore;
  /** `leftmode`, for the trigger's current label. */
  leftMode: string | undefined;
}

function ModeIcon({ entry }: { entry: Pick<TemplateEntry, "mode" | "icon"> }) {
  const url = paneModeIconUrl(entry.icon);
  // The template's own icon.svg, drawn the way the shell draws it
  // (`mode-icon-mask`): a MASK filled with currentColor, so one flat glyph
  // inherits the row's ink and both themes instead of shipping two colourways.
  if (!url) {
    return (
      <span className="c-mode-ic c-mode-ic-letter" aria-hidden="true">
        {paneModeLetter(entry.mode)}
      </span>
    );
  }
  return (
    <span
      className="c-mode-ic"
      aria-hidden="true"
      style={{ WebkitMaskImage: url, maskImage: url }}
    />
  );
}

export function LeftModePicker({ modes, params, leftMode }: LeftModePickerProps) {
  // Nothing to pick, no control (see `pickerHost`).
  if (modes.length < 2) return null;
  const current = curLeftEntry(modes, leftMode);
  if (!current) return null;

  return (
    <div className="c-leftmode">
      <DropdownMenu>
        <DropdownMenuTrigger
          className="c-leftmodebtn"
          // The trigger names the control, not the value: the value is on screen
          // beside it (T:4054).
          aria-label="Preview pane view"
        >
          <ModeIcon entry={current} />
          <span className="c-mode-lbl">{paneModeLabel(current.mode)}</span>
          <span className="c-mode-chev" aria-hidden="true">
            ▾
          </span>
        </DropdownMenuTrigger>
        <DropdownMenuContent className="c-leftmodepop" align="end" sideOffset={6}>
          <DropdownMenuRadioGroup
            value={current.mode}
            onValueChange={(mode) => {
              // PARAM ONLY. `useFramedSrc` swaps the iframe from the same param,
              // so a click, a reload and a later param write are one path
              // (T:5580).
              params.set({ leftmode: mode }, { history: "replace" });
            }}
          >
            {modes.map((e) => (
              <DropdownMenuRadioItem key={e.mode} value={e.mode} className="c-mode-opt">
                <ModeIcon entry={e} />
                <span className="c-mode-lbl">{paneModeLabel(e.mode)}</span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
