// One ranked result. The match strength is drawn as a WASH behind the line
// rather than as a bar beside it: the row is already a line of text and a
// figure, and a third column of chart would make the strongest thing on the row
// the one nobody reads. The wash is scaled to the best match, so the top row is
// always full and the rest read relative to it.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

/** The embed stage's own column gap — wider than the other four, because its
 *  result is a LIST and a list needs air above it. */
export const embedStageClass = "gap-[14px]";

export function EmbedResults({ className, ...props }: ComponentProps<"ol">) {
  return (
    <ol className={cn("m-0 flex list-none flex-col gap-1.5 p-0", className)} {...props} />
  );
}

export function EmbedRow({
  /** A thumbnail has no baseline, so a row mixing one with a filename would
   *  hang the picture off the text's. */
  media,
  className,
  ...props
}: ComponentProps<"li"> & { media?: boolean }) {
  return (
    <li
      className={cn(
        "relative flex gap-3 overflow-hidden rounded-[8px] border border-solid border-[var(--border)] px-3 py-2 text-[13px]",
        media ? "items-center" : "items-baseline",
        className,
      )}
      {...props}
    />
  );
}

/** Width is inline, from the score — the one thing on this row a stylesheet
 *  cannot know. */
export function EmbedBar({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "pointer-events-none absolute inset-y-0 left-0 bg-[rgba(var(--accent-rgb),0.1)]",
        className,
      )}
      {...props}
    />
  );
}

export function EmbedText({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("relative min-w-0 flex-1", className)} {...props} />;
}

export function EmbedScore({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "relative flex-none text-[11.5px] text-[var(--fg-muted)] tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

/** The chosen files, behind the settings cog — the slot the text mode's corpus
 *  textarea occupies. */
export function EmbedPictures({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-col items-start gap-1", className)} {...props} />
  );
}

export function EmbedPictureRow({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex w-full items-center gap-2 text-[12px]", className)} {...props} />
  );
}

/** The path is the tooltip; the row shows the basename. A photo library's paths
 *  are long enough that showing them would push Remove off the panel. */
export function EmbedPictureName({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("min-w-0 flex-1 truncate", className)} {...props} />;
}

export const embedThumbClass =
  "relative h-10 w-10 flex-none rounded-[5px] bg-[var(--bg-subtle)] object-cover";
