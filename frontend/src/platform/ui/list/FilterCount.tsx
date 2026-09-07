// The little count on a filter trigger ("Status 2"): an accent-tinted pill at
// micro size, 16px tall, centred. Was `.schedule-tv-filter-count`.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";

export function FilterCount({ className, ...props }: ComponentProps<typeof Badge>) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "schedule-tv-filter-count h-4 min-w-4 justify-center rounded-pill border-0 bg-[rgba(var(--accent-rgb),0.16)] px-1.5 py-0 text-micro font-semibold leading-4 text-[var(--accent-soft)]",
        className,
      )}
      {...props}
    />
  );
}
