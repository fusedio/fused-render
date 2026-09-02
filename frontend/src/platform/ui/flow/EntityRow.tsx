// Flow composite: the universal list row. Compose primitives, never fork them.
// Slots left→right: leading (status icon / glyph), identifier (mono), title,
// meta, trailing (badge / timestamp) pinned right. Rows group inside
// <EntityList>, which draws the shared border and squares the corners.
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@platform/lib/utils";

export function EntityList({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="entity-list"
      className={cn("border border-border rounded-lg bg-card overflow-hidden", className)}
      {...props}
    />
  );
}

type RowProps = {
  leading?: ReactNode;
  id?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  selected?: boolean;
  onClick?: () => void;
  href?: string;
  className?: string;
  children?: ReactNode;
};

export function EntityRow({ leading, id, title, meta, trailing, selected, onClick, href, className, children }: RowProps) {
  const interactive = Boolean(onClick || href);
  const cls = cn(
    "flex items-center gap-3 px-4 py-2 text-sm border-b border-border last:border-b-0 min-w-0 text-left w-full",
    interactive && "cursor-pointer hover:bg-accent/50 focus-visible:outline-none focus-visible:bg-accent/50",
    selected && "bg-accent/30",
    className,
  );
  const body = (
    <>
      {leading != null && <span className="shrink-0 flex items-center text-muted-foreground">{leading}</span>}
      {id != null && <span className="shrink-0 font-mono text-xs text-muted-foreground">{id}</span>}
      <span className="flex-1 min-w-0 flex items-center gap-2">
        <span className="font-medium truncate">{title}</span>
        {meta != null && <span className="text-xs text-muted-foreground truncate">{meta}</span>}
      </span>
      {children}
      {trailing != null && <span className="shrink-0 flex items-center gap-2 text-xs text-muted-foreground">{trailing}</span>}
    </>
  );
  if (href) {
    return (
      <a data-slot="entity-row" href={href} className={cls} aria-current={selected || undefined}>
        {body}
      </a>
    );
  }
  if (onClick) {
    return (
      <button type="button" data-slot="entity-row" onClick={onClick} className={cls} aria-pressed={selected}>
        {body}
      </button>
    );
  }
  return (
    <div data-slot="entity-row" className={cls}>
      {body}
    </div>
  );
}
