// The stage's title row: the action named on the left, the settings cog at the
// far right end of the same line. The cog lives up here rather than under the
// input because the title row is the stage's own header — the settings belong
// to the stage, not to the prompt — and a toggle at the end of a heading is
// where a settings affordance is looked for.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export function StageHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex items-center justify-between gap-3", className)} {...props} />;
}

/** One line naming the action — the hero card above carries the model. */
export function StageTitle({ className, ...props }: ComponentProps<"h2">) {
  return (
    <h2 className={cn("m-0 text-[15px] font-semibold tracking-[-0.01em]", className)} {...props} />
  );
}

/** Quiet until wanted: a muted glyph that lights on hover and stays lit while
 *  the panel it opened is on screen.
 *
 *  The glyph turns one notch while the panel opens — 45° and not some other
 *  angle because the gear has eight lobes: the shape it RESTS in is identical
 *  to the shape it left, so the turn is motion the eye catches and never a
 *  tilted icon left behind. It rides `--pg-glide`, the same beat as the track
 *  it opens, so the two read as one movement. */
export const configCogVariants = cva(
  "inline-flex h-[26px] w-[26px] flex-none cursor-pointer items-center justify-center rounded-[6px] border-none bg-transparent p-0 " +
    "hover:bg-[rgba(var(--tint),0.07)] hover:text-[var(--fg)] " +
    "[&_svg]:transition-[transform] [&_svg]:duration-[var(--pg-glide)] [&_svg]:ease-[cubic-bezier(0.2,0.7,0.3,1)] " +
    "motion-reduce:[&_svg]:transition-none",
  {
    variants: {
      active: {
        true: "text-[var(--accent)] [&_svg]:[transform:rotate(45deg)]",
        false: "text-[var(--fg-muted)]",
      },
    },
    defaultVariants: { active: false },
  },
);

export function ConfigCog({
  active,
  className,
  ...props
}: ComponentProps<"button"> & VariantProps<typeof configCogVariants>) {
  return <button className={cn(configCogVariants({ active, className }))} {...props} />;
}
