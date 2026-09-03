// The 16px glyph slot every mode surface renders a template icon into — the
// mode control's trigger, the mode dropdown's rows, the folder pane's Preview
// toggle. Replaces the `.mode-menu-icon` / `.bar-menu-item-icon` rules.
//
// It sizes its CHILD by class name on purpose. What lands in here is whatever
// ModeSwitcher.templateModeIcon returned: an inline SVG, a `.mode-icon-mask`
// span (a monochrome template icon tinted through mask-image + currentColor),
// a `.mode-icon-placeholder` box, or a `.mode-icon-spinner`. Those three class
// names are NOT ours to retire — shell/App.tsx and shell/AppFiles.tsx render
// them too, so their rules stay in preview.css — and a mask span has no
// intrinsic size, so the slot has to give it one.
import type { ComponentProps } from "react";
import { cn } from "@platform/lib/utils";

export function ModeGlyph({ className, dense = false, ...props }: ComponentProps<"span"> & { dense?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center",
        dense
          ? "size-3.5 [&_.mode-icon-mask]:size-3.5 [&_.mode-icon-placeholder]:size-3.5 [&_svg]:size-3.5"
          : "size-4 [&_.mode-icon-mask]:size-4 [&_.mode-icon-placeholder]:size-4 [&_svg]:size-4",
        // The spinner is always a step smaller than the glyph it stands in for:
        // a ring at the full 16px reads as a bigger object than the icon it
        // replaces, and the swap flickers.
        "[&_.mode-icon-spinner]:size-3 [&_.mode-icon-spinner]:border-[1.5px]",
        className,
      )}
      {...props}
    />
  );
}
