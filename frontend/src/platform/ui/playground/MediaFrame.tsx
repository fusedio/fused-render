// A rendered picture or clip, framed — the image and video stages' result box.
//
// Width and aspect ratio come from the RUN (the stage's `shot` style), and
// everything inside fills the box, so no state resizes the column: the shimmer
// that waits, the live preview that fades in, and the final file all draw at
// exactly the same size.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";
import { Skeleton } from "@platform/shadcn/ui/skeleton";

import { BARE_BUTTON } from "./classes";

/** The result, as a figure: frame, then caption. */
export function MediaResult({ className, ...props }: ComponentProps<"figure">) {
  return <figure className={cn("m-0 flex flex-col gap-2", className)} {...props} />;
}

/** `leading-[0]` is what keeps the line box from adding a few pixels under an
 *  inline-level media element inside the frame. */
export function MediaFrame({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("relative max-w-full self-start leading-[0]", className)} {...props} />
  );
}

/** The framed media itself. `video` gets the identical treatment: a
 *  `<video controls>` left unstyled sizes to its own intrinsic pixels rather
 *  than to the aspect-ratio box the frame just set. */
export const mediaClass =
  "block h-full w-full rounded-[12px] border border-solid border-[var(--border)] bg-[var(--bg-alt)] object-contain";

/** The "gone means done" fallback failing its own artefact check — the same
 *  muted-note treatment the transcript stage's empty result gets. */
export function MediaReadFailed({ className, ...props }: ComponentProps<"p">) {
  return (
    <p
      className={cn("m-0 px-4 py-6 text-[13px] leading-[1.5] text-[var(--fg-muted)]", className)}
      {...props}
    />
  );
}

/** The wait: a sweeping band, on shadcn's `Skeleton` with its pulse traded for
 *  the sweep — a pulse says "loading a row of text", a sweep says "a picture is
 *  being drawn". */
export function MediaWait({ className, ...props }: ComponentProps<"div">) {
  return (
    <Skeleton
      className={cn(
        "h-full w-full rounded-[12px] border border-solid border-[var(--border)]",
        "bg-[linear-gradient(100deg,var(--bg-alt)_40%,var(--bg)_50%,var(--bg-alt)_60%)] [background-size:200%_100%]",
        "animate-pg-shimmer motion-reduce:animate-none",
        className,
      )}
      aria-hidden="true"
      {...props}
    />
  );
}

/** Progress only: drawn while the job runs, gone when it lands. */
export function MediaCaption({ className, ...props }: ComponentProps<"figcaption">) {
  return (
    <figcaption
      className={cn("flex flex-col gap-1.5 text-[12px] text-[var(--fg-muted)] tabular-nums", className)}
      {...props}
    />
  );
}

/** "Reuse this seed" — a dotted-underline word in the caption, quieter than a
 *  button because it is a fact you may click, not an action the stage offers. */
export function SeedButton({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        BARE_BUTTON,
        "text-[var(--fg-muted)] underline decoration-dotted hover:text-[var(--fg)]",
        className,
      )}
      {...props}
    />
  );
}
