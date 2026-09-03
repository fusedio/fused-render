// The record button: idle = a ring and a red dot; live = a red square and a
// pulse that rings outward. The state IS the treatment.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export const recordButtonVariants = cva(
  "inline-flex h-[52px] w-[52px] flex-none cursor-pointer items-center justify-center rounded-[50%] border-2 border-solid bg-[var(--bg-alt)] " +
    "disabled:cursor-default disabled:opacity-50",
  {
    variants: {
      live: {
        true: "border-[var(--error)] animate-pg-pulse motion-reduce:animate-none",
        false: "border-[var(--border)] [&:hover:not(:disabled)]:border-[var(--accent)]",
      },
    },
    defaultVariants: { live: false },
  },
);

export function RecordButton({
  live,
  className,
  ...props
}: ComponentProps<"button"> & VariantProps<typeof recordButtonVariants>) {
  return <button className={cn(recordButtonVariants({ live, className }))} {...props} />;
}

export function RecordDot({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("h-[18px] w-[18px] rounded-[50%] bg-[var(--error)]", className)}
      {...props}
    />
  );
}

export function RecordSquare({ className, ...props }: ComponentProps<"span">) {
  return (
    <span className={cn("h-4 w-4 rounded-[3px] bg-[var(--error)]", className)} {...props} />
  );
}

/** The recording, in progress: the dropzone's box in the error hue, so the
 *  substitution reads as the same slot rather than as a new panel. */
export function RecordingRow({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex items-center gap-4 rounded-[12px] border-[1.5px] border-solid border-[var(--error)] bg-[rgba(var(--error-rgb),0.05)] p-[18px]",
        className,
      )}
      {...props}
    />
  );
}

export function RecordInfo({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-wrap items-center gap-[14px]", className)} {...props} />
  );
}

export function RecordTime({ className, ...props }: ComponentProps<"span">) {
  return (
    <span className={cn("text-[16px] font-semibold tabular-nums", className)} {...props} />
  );
}

export function RecordHint({ className, ...props }: ComponentProps<"span">) {
  return (
    <span className={cn("text-[12.5px] text-[var(--fg-muted)]", className)} {...props} />
  );
}
