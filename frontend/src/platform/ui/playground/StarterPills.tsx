// Example prompts, as one row of outlined pills under the input: an icon and a
// short name each, with a round rotate button at the end.
//
// Pills rather than filled cards: a card carries a picture and a meta row, and
// these carry two words — at pill size a fill and a shadow read as four heavy
// blocks under a quiet input. Border and text only on hover for the same
// reason: a tinted plate under the hovered one of eight reads as a selected
// state rather than as a pointer.
//
// Each pill HUGS its name instead of taking an equal grid column: equal columns
// are as wide as the longest name and truncate every other one, which is the
// one thing a two-word label cannot afford. Nothing here ellipsises — when the
// row runs out of width the COUNT drops instead. That measurement lives with
// the caller (it owns the ref and the layout effect); `overflow-hidden` on the
// grid is what makes it possible (scrollWidth vs clientWidth) and is never
// seen.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

import { INHERIT_FONT } from "./classes";

/** The row. `p-0.5` is room for the focus ring. */
export const starterRowClass = "flex items-start gap-2 p-0.5";

/** The measured box. `flex: 0 1 auto`, NOT `flex: 1`: the row is as wide as its
 *  pills, so the rotate button sits immediately after the last one instead of
 *  being parked at the column's right edge with a hole in between. It still
 *  SHRINKS below that, which is what keeps the caller's measurement meaningful
 *  — a row that could not shrink would never report an overflow and the count
 *  would never drop. */
export const starterGridClass =
  "flex min-w-0 flex-[0_1_auto] flex-nowrap gap-2 overflow-hidden";

export function StarterPill({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "group/starter flex flex-none cursor-pointer items-center gap-2 rounded-[999px] border border-solid border-[var(--border)] bg-transparent px-3.5 py-[7px] text-left text-[12.5px] whitespace-nowrap text-[var(--fg-muted)]",
        INHERIT_FONT,
        "transition-[color,border-color] duration-[.12s] ease-[ease]",
        "hover:border-[var(--accent)] hover:text-[var(--fg)]",
        className,
      )}
      {...props}
    />
  );
}

/** A shade quieter than the name at rest — the glyph is for scanning the row,
 *  the name is what is being read. */
export function StarterIcon({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "flex flex-none opacity-75",
        "group-hover/starter:text-[var(--accent)] group-hover/starter:opacity-100",
        className,
      )}
      aria-hidden="true"
      {...props}
    />
  );
}

/** Round, and the pill's own height: it is chrome on the row, not a ninth
 *  example. */
export function StarterRotate({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "flex h-[33px] w-[33px] flex-none cursor-pointer items-center justify-center rounded-[50%] border border-solid border-[var(--border)] bg-transparent p-0 text-[var(--fg-muted)]",
        "[&_svg]:h-[15px] [&_svg]:w-[15px]",
        "hover:border-[var(--accent)] hover:bg-[rgba(var(--accent-rgb),0.08)] hover:text-[var(--fg)]",
        className,
      )}
      {...props}
    />
  );
}
