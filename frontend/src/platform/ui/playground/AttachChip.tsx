// The picked photo, as a chip on the composer's floor: the thumbnail and the
// way to drop it, nothing else. It takes the empty half of the floor
// (`mr-auto`), so the picture sits on the same line as the buttons that put it
// there.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

import { INHERIT_FONT_FACE } from "./classes";

/** The row the chip and the attach buttons share. */
export function AttachRow({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-wrap items-center gap-2 px-1 py-0", className)} {...props} />
  );
}

export function AttachChip({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "mr-auto inline-flex items-center gap-1 rounded-[8px] border border-solid border-[var(--border)] bg-[var(--bg)] p-[3px]",
        className,
      )}
      {...props}
    />
  );
}

/** The thumbnail is a BUTTON, because it opens the picture at full size — no
 *  frame of its own, so what is drawn is still just the photo.
 *
 *  28px, so the chip (border + 3px of padding around it) comes out at the 32px
 *  height of the Generate button it shares the floor with — a taller thumbnail
 *  set the whole row's height and pushed the box down. The flat ground under it
 *  is checker-free on purpose: a transparent PNG over the composer's own fill
 *  would read as part of the box rather than as a picture in it. */
export function AttachOpen({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "block cursor-zoom-in rounded-[5px] border-none bg-transparent p-0 leading-none",
        "[&_img]:h-7 [&_img]:w-7 [&_img]:rounded-[5px] [&_img]:bg-[rgba(var(--tint),0.08)] [&_img]:object-cover",
        "hover:[&_img]:opacity-[0.82]",
        className,
      )}
      {...props}
    />
  );
}

/** The ✕ that removes the picture. */
export function AttachDrop({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "h-[22px] w-[22px] flex-none cursor-pointer rounded-[5px] border-none bg-transparent text-xs leading-none text-[var(--fg-muted)]",
        INHERIT_FONT_FACE,
        "hover:bg-[rgba(var(--tint),0.1)] hover:text-[var(--fg)]",
        className,
      )}
      {...props}
    />
  );
}

export function AttachNote({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("text-[11.5px] text-[var(--fg-muted)]", className)} {...props} />;
}
