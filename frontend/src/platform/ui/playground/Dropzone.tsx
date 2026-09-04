// The transcribe stage's way in: drop a file, browse for one, or record. Three
// explicit states — idle dashed, dragging accent, busy solid — because the
// border style is the only thing on this box that can say "not now" without
// moving anything.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export const dropzoneVariants = cva(
  "flex items-center gap-4 rounded-[12px] border-[1.5px] p-[18px]",
  {
    variants: {
      dragging: {
        true: "border-[var(--accent)] bg-[rgba(var(--accent-rgb),0.06)]",
        false: "border-[var(--border)] bg-[rgba(var(--tint),0.02)]",
      },
      /** Solid, not dashed: a dashed edge invites a drop, and while a job runs
       *  the drop is ignored. */
      busy: { true: "border-solid", false: "border-dashed" },
    },
    defaultVariants: { dragging: false, busy: false },
  },
);

export function Dropzone({
  dragging,
  busy,
  className,
  ...props
}: ComponentProps<"div"> & VariantProps<typeof dropzoneVariants>) {
  return <div className={cn(dropzoneVariants({ dragging, busy, className }))} {...props} />;
}

export function DropCopy({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex min-w-0 flex-1 flex-col gap-1", className)} {...props} />
  );
}

export function DropTitle({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("m-0 text-[13.5px] font-semibold", className)} {...props} />;
}

export function DropSub({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("m-0 text-[12.5px] text-[var(--fg-muted)]", className)} {...props} />;
}

/** A <label> lying over a file input that covers it completely — the whole word
 *  is the hit area, and the input itself is never seen. */
export function BrowseLabel({ className, ...props }: ComponentProps<"label">) {
  return (
    <label
      className={cn(
        "relative cursor-pointer text-[var(--accent-soft)] underline decoration-dotted",
        "[&_input[type=file]]:absolute [&_input[type=file]]:inset-0 [&_input[type=file]]:w-full [&_input[type=file]]:cursor-pointer [&_input[type=file]]:opacity-0",
        className,
      )}
      {...props}
    />
  );
}
