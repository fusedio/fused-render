// A scrollable, mono-metadata table with a sticky header — replaces
// `.index-query-results` / `-results table` / `-results th` /
// `-results tr:last-child td`. Thin wrapper around shadcn `Table`; callers
// still compose `TableHeader`/`TableBody`/`TableRow` from
// `@platform/shadcn/ui/table` directly, using the two class helpers below on
// the header/body cells (measurements are mono per the design system's
// mono-for-measurement rule, and this table is nothing BUT measurements).
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";
import { Table } from "@platform/shadcn/ui/table";

export function DataTable({ className, children, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("max-h-[420px] overflow-auto rounded-control border border-border", className)}
      {...props}
    >
      <Table className="min-w-full font-mono text-meta">{children}</Table>
    </div>
  );
}

// Apply to every `TableHead`: sticky, muted, semibold — the header survives
// the container's own scroll.
export const dataTableHeadClass =
  "sticky top-0 z-10 bg-muted font-mono text-meta font-semibold text-muted-foreground";

// Apply to every `TableCell`.
export const dataTableCellClass = "font-mono text-meta";
