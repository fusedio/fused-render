// A row of sibling action buttons — replaces `.prefs-actions`. Overrides
// `Button`'s own `align-self` intent so buttons sit side by side and wrap on
// a narrow viewport.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function ActionRow({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)} {...props} />
  );
}
