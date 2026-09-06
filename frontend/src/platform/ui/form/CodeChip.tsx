// Inline `<code>` — replaces `.prefs-section code`. `overflow-wrap: anywhere`
// (not `break-all`) deliberately, so a long path/env-var name wraps at a
// natural break before it resorts to splitting mid-word.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function CodeChip({ className, ...props }: ComponentProps<"code">) {
  return (
    <code
      className={cn(
        "rounded-xs border border-border bg-background px-1.5 py-px text-meta [overflow-wrap:anywhere]",
        className,
      )}
      {...props}
    />
  );
}
