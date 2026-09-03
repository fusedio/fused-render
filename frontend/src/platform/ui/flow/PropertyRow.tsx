// Flow composite: label-left / value-right property row, stacked in a w-80
// right-side properties panel on detail views.
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@platform/lib/utils";

export function PropertyList({ className, ...props }: ComponentProps<"dl">) {
  return <dl data-slot="property-list" className={cn("space-y-1", className)} {...props} />;
}

export function PropertyRow({ label, children, className }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div data-slot="property-row" className={cn("flex items-start justify-between gap-3 py-1.5 text-sm", className)}>
      <dt className="text-xs text-muted-foreground shrink-0 pt-px">{label}</dt>
      <dd className="text-right min-w-0 truncate">{children}</dd>
    </div>
  );
}

export function PropertiesPanel({ className, ...props }: ComponentProps<"aside">) {
  return (
    <aside
      data-slot="properties-panel"
      className={cn("w-80 shrink-0 border-l border-border bg-background p-4 overflow-y-auto scrollbar-auto-hide", className)}
      {...props}
    />
  );
}
