// THE control recipe every explorer bar is built from — the crumb bar, the
// listing's search row, the preview pane header, the preview sidebar header and
// the split panel's pane bars. It replaces the `.bar-ctl` / `.bar-ctl-icon` /
// `.bar-ctl-bordered` / `.bar-ctl-strong` class family that used to live in
// explorer.css, on the shadcn Button primitive.
//
// Four tones, and the reservation between them is the point:
//
//   ghost     the default. Muted glyph or label, hover wash, no box. Nearly
//             everything in these rows is this: the bars are chrome, and the
//             subject is what the view is showing.
//   bordered  the ONE thing in a bar that gets chrome — the mode control.
//             A hairline says "this one opens a menu".
//   strong    the row's single filled action ("Open app" in the pane strip).
//             `bg-primary` is the theme's own text-on-surface pair, so it
//             reads loudest in both themes by construction. Deliberately not a
//             chromatic accent: status colour is single-sourced and this is
//             not a status.
//   plain     no size/tone at all — for a trigger that must inherit the box of
//             whatever it is nested in.
//
// `dense` re-scales the whole set from 28px to 24px, which is what the split
// panel's pane bars need (they used to get it from a `.panel-bar .bar-ctl`
// override in preview.css).
import type { ComponentProps } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";

export type BarTone = "ghost" | "bordered" | "strong";

const TONE_VARIANT = {
  ghost: "ghost",
  bordered: "outline",
  strong: "default",
} as const;

type BarButtonProps = Omit<ComponentProps<typeof Button>, "variant" | "size"> & {
  tone?: BarTone;
  /** Square glyph-only box: same height, no label, no horizontal padding. */
  icon?: boolean;
  /** 24px instead of 28px — the split panel's pane bars. */
  dense?: boolean;
};

export function BarButton({ tone = "ghost", icon = false, dense = false, className, ...props }: BarButtonProps) {
  return (
    <Button
      variant={TONE_VARIANT[tone]}
      size={icon ? (dense ? "icon-xs" : "icon-sm") : dense ? "xs" : "sm"}
      className={cn(
        // `text-xs` over the size variant's own step: the neighbour in these
        // rows is 12px monospace path text, not another control.
        "shrink-0 gap-1.5 text-xs font-normal disabled:opacity-40 [&_svg:not([class*='size-'])]:size-4",
        !icon && (dense ? "px-1.5" : "px-2"),
        dense && "text-[11px] [&_svg:not([class*='size-'])]:size-3.5",
        tone === "ghost" && "text-muted-foreground",
        tone === "bordered" && "font-medium text-foreground",
        tone === "strong" && "font-semibold",
        className,
      )}
      {...props}
    />
  );
}

// A run of glyph buttons that has to read as ONE control (the ‹ › history pair,
// the panel's layout group). 2px between them is what welds them; anything
// looser and they read as two separate buttons that happen to be adjacent.
export function BarZone({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex shrink-0 items-center gap-0.5", className)} {...props} />;
}

// Hairline between two zones in one bar. A zone divider, not decoration: it is
// what makes "mode" and "layout" read as two things rather than six buttons.
export function BarRule({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      aria-hidden="true"
      className={cn("mx-2.5 h-[18px] w-px shrink-0 bg-border", className)}
      {...props}
    />
  );
}

// The far end of a side column's header strip, packed by an auto margin on THIS
// wrapper rather than on whichever leading item happens to render last. That is
// load-bearing: the left end holds a variable number of things (the pane's also
// carries the open folder's primary action, portaled in and usually absent), and
// an auto margin on a variable last child distributes the slack differently
// every time — with two present the middle one drifts to the centre.
//
// `empty:hidden`, because an empty wrapper would still spend one of the strip's
// gaps (the pane's non-Preview states have no expand button to put here).
export function BarTail({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("ml-auto flex shrink-0 items-center gap-2 empty:hidden", className)}
      {...props}
    />
  );
}
