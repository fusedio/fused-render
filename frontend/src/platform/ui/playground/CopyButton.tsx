// Copy, as an icon in a result card's top-right corner — and, in the `scrim`
// variant, the Save link pinned to a rendered picture's corner.
//
// The two are one control with two grounds. The default sits on the app's own
// surfaces and paints only on hover; the scrim variant sits on ARBITRARY PHOTO
// PIXELS, so its colours come from the `--scrim-*` pair, which is identical in
// both palettes by design. They are tokens all the same — "every colour comes
// from the palette" is a rule with no exceptions to remember.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export const copyButtonVariants = cva(
  "absolute top-2.5 right-2.5 grid h-[26px] w-[26px] cursor-pointer place-items-center rounded-[6px] border border-solid p-0 " +
    "[&_svg]:h-3.5 [&_svg]:w-3.5",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-transparent text-[var(--fg-muted)] " +
          "hover:border-[var(--border)] hover:bg-[rgba(var(--tint),0.06)] hover:text-[var(--fg)]",
        scrim:
          "border-[var(--scrim-border)] bg-[var(--scrim-bg)] text-[var(--scrim-fg)] no-underline " +
          "hover:border-[var(--scrim-border-hover)] hover:bg-[var(--scrim-bg-hover)] hover:text-[var(--scrim-fg)]",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export function CopyButton({
  variant,
  className,
  ...props
}: ComponentProps<"button"> & VariantProps<typeof copyButtonVariants>) {
  return <button className={cn(copyButtonVariants({ variant, className }))} {...props} />;
}
