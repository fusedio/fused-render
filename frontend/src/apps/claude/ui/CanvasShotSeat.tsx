// The Screenshot seat on a canvas page whose host offers MORE THAN ONE picture
// (`CanvasShot.targets`: the canvas view, or this UDF's output): the same seat
// face, opening a two-row menu in the kebab's own dropdown dress. With one
// target the plain `ScreenshotSeat` is used instead — no menu for one choice.
import { useState } from "react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import { CANVAS_SHOT_LABEL, type CanvasShotWhat } from "../shots/canvas-shot";
import { ScreenshotGlyph } from "./AnnStrip";

export function CanvasShotSeat({
  targets,
  disabled = false,
  inert = false,
  capturing = false,
  onPick,
}: {
  targets: readonly CanvasShotWhat[];
  disabled?: boolean;
  inert?: boolean;
  capturing?: boolean;
  onPick: (what: CanvasShotWhat) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            className={"c-viewshot" + (capturing ? " is-capturing" : "")}
            aria-label="Screenshot the canvas and attach it to this message"
            title="Screenshot the canvas and attach it to this message"
            aria-disabled={inert ? "true" : "false"}
            aria-busy={capturing ? "true" : "false"}
            disabled={disabled}
          >
            <ScreenshotGlyph capturing={capturing} />
          </button>
        }
      />
      <DropdownMenuContent
        side="bottom"
        align="end"
        sideOffset={6}
        aria-label="What to capture"
        className="c-overlay c-kebabpop w-auto min-w-[196px] rounded-[10px] bg-[var(--c-panel)] p-1 text-[var(--c-fg)] shadow-none ring-0"
      >
        {targets.map((what) => (
          <DropdownMenuItem
            key={what}
            className="c-kebab-opt"
            onClick={() => {
              setOpen(false);
              onPick(what);
            }}
          >
            {CANVAS_SHOT_LABEL[what]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
