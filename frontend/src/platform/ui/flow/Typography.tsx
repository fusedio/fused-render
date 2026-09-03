// Flow typography scale (SKILL.md Layer 1 §5). Use these instead of inventing
// a size/weight combo: page title xl/bold; section title lg/semibold; section
// heading sm/semibold muted uppercase tracking-wide; row title sm/medium; body
// sm; muted sm; tiny metadata xs muted; identifiers xs mono muted; stat 2xl/bold.
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@platform/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header data-slot="page-header" className={cn("flex items-start justify-between gap-4 px-6 py-4 border-b border-border", className)}>
      <div className="min-w-0">
        <h1 className="m-0 text-xl font-bold leading-tight truncate">{title}</h1>
        {description != null && <p className="text-sm text-muted-foreground mt-0.5">{description}</p>}
      </div>
      {actions != null && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  );
}

export function SectionTitle({ className, ...props }: ComponentProps<"h2">) {
  return <h2 className={cn("m-0 text-lg font-semibold", className)} {...props} />;
}

export function SectionHeading({ className, ...props }: ComponentProps<"h3">) {
  return (
    <h3 className={cn("m-0 text-sm font-semibold text-muted-foreground uppercase tracking-wide", className)} {...props} />
  );
}

export function Muted({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function Tiny({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("text-xs text-muted-foreground", className)} {...props} />;
}

export function Identifier({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("font-mono text-xs text-muted-foreground", className)} {...props} />;
}

export function Stat({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("text-2xl font-bold tabular-nums", className)} {...props} />;
}

export function Page({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="page" className={cn("flex flex-col min-h-0 h-full bg-background text-foreground", className)} {...props} />;
}

export function PageBody({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="page-body" className={cn("flex-1 min-h-0 overflow-y-auto scrollbar-auto-hide px-6 py-4 space-y-6", className)} {...props} />;
}

export function MetricGrid({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("grid md:grid-cols-2 xl:grid-cols-4 gap-4", className)} {...props} />;
}
