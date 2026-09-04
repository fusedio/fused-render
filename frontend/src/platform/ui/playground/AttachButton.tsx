// The two ways to bring a picture in — a <button> that opens the native picker
// and a <label> over a hidden file input — wear the SAME pill, on purpose: a
// person reading the row must not be able to tell which is which.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

import { INHERIT_FONT } from "./classes";

export const attachButtonVariants = cva(
  "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-[999px] border border-solid px-2.5 text-xs " +
    INHERIT_FONT +
    " [&_input[type=file]]:hidden",
  {
    variants: {
      /** Lit while the thing it opened is on screen — the webcam viewfinder is
       *  the only one that has an "on". */
      active: {
        true: "border-[var(--accent)] bg-transparent text-[var(--accent)]",
        false:
          "border-[var(--border)] bg-transparent text-[var(--fg-muted)] " +
          "hover:border-[var(--fg-muted)] hover:text-[var(--fg)]",
      },
    },
    defaultVariants: { active: false },
  },
);

export function AttachButton({
  active,
  className,
  ...props
}: ComponentProps<"button"> & VariantProps<typeof attachButtonVariants>) {
  return <button className={cn(attachButtonVariants({ active, className }))} {...props} />;
}
