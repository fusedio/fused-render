// The recording itself, playable, with the re-run action beside it — the
// compare loop's home. ONE bordered row, so it reads as a fact about the run
// rather than as another dropzone.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function AudioRow({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-2.5 rounded-[10px] border border-solid border-[var(--border)] bg-[var(--bg-alt)] px-[14px] py-2.5",
        className,
      )}
      {...props}
    />
  );
}

export function AudioMeta({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-0.5", className)} {...props} />
  );
}

export function AudioLabel({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "text-[11px] font-semibold tracking-[0.05em] text-[var(--fg-muted)] uppercase",
        className,
      )}
      {...props}
    />
  );
}

/** The filename, capped: a path from a photo library is long enough to push the
 *  player off the row. */
export function AudioName({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("max-w-[220px] truncate text-[12px] text-[var(--fg-muted)]", className)}
      {...props}
    />
  );
}

/** The native player. It keeps its own UA chrome — a hand-drawn transport for
 *  one 20-second clip is a control surface nobody asked for. */
export const audioPlayerClass = "h-9 min-w-[200px] flex-[1_1_240px]";
