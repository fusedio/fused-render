// The strip of past clips under a video result: a horizontal scroller of
// thumbnails, quiet at 80% until the pointer or the selection brings one
// forward.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export function MediaStrip({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex gap-2 overflow-x-auto pb-0.5", className)} {...props} />;
}

export const mediaStripItemVariants = cva(
  "h-[84px] cursor-pointer rounded-[8px] border border-solid",
  {
    variants: {
      active: {
        true: "border-[var(--accent)] opacity-100",
        false: "border-[var(--border)] opacity-80 hover:opacity-100",
      },
      /** A render is in flight: picking a past clip would silently drop the
       *  in-flight Stop button and its progress. */
      disabled: { true: "pointer-events-none cursor-not-allowed opacity-40", false: "" },
    },
    defaultVariants: { active: false, disabled: false },
  },
);

export function mediaStripItemClass(
  options: VariantProps<typeof mediaStripItemVariants>,
  className?: string,
): string {
  return cn(mediaStripItemVariants(options), className);
}
