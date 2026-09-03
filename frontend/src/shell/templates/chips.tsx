// Small chips shared across the Templates surface.
//   TemplateChip — a template name in an ordered list: `default` marks the
//     first, `broken` a name no folder resolves to, optional ✕ removes it,
//     optional drag handlers reorder it (RowEditorModal).
//   KeyPill — a registry key (".csv", "dir/") as a mono identifier pill.
//   FilterGroup — the single-select segmented control both tab toolbars use.
import type { ComponentProps, ReactNode } from "react";
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { statusText } from "@platform/ui/status-colors";

export function TemplateChip({
  name,
  isDefault = false,
  broken = false,
  small = false,
  title,
  onRemove,
  removeLabel,
  removeDisabled,
  className,
  ...rest
}: {
  name: ReactNode;
  isDefault?: boolean;
  broken?: boolean;
  small?: boolean;
  title?: string;
  onRemove?: () => void;
  removeLabel?: string;
  removeDisabled?: boolean;
} & Omit<ComponentProps<"span">, "title">) {
  return (
    <span
      data-slot="template-chip"
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border border-border bg-card font-mono",
        small ? "h-5 px-1.5 text-xs" : "h-7 px-2 text-xs",
        broken && "border-dashed text-destructive",
        rest.draggable && "cursor-grab active:cursor-grabbing",
        className,
      )}
      {...rest}
    >
      {isDefault && (
        <span className="rounded-full bg-primary px-1.5 text-[10px] font-sans font-medium leading-4 text-primary-foreground">
          default
        </span>
      )}
      <span className="truncate">{name}</span>
      {onRemove && (
        <button
          type="button"
          className="-mr-1 inline-flex size-4 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
          onClick={onRemove}
          disabled={removeDisabled}
          aria-label={removeLabel ?? "Remove"}
          title={removeLabel ?? "Remove"}
        >
          <XIcon className="size-3" />
        </button>
      )}
    </span>
  );
}

export function KeyPill({ className, ...props }: ComponentProps<"code">) {
  return (
    <code
      data-slot="key-pill"
      className={cn(
        "inline-flex h-5 items-center rounded-md border border-border bg-muted px-1.5 font-mono text-xs text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function WarnText({ className, children, title }: { className?: string; children: ReactNode; title?: string }) {
  return (
    <div className={cn("text-xs", statusText("warning"), className)} title={title}>
      {children}
    </div>
  );
}

// Single-select segmented filter. base-ui's ToggleGroup reports an array and
// yields [] when the active item is re-pressed — ignore that so the selection
// stays sticky.
export function FilterGroup<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode; title?: string; className?: string }[];
  ariaLabel: string;
  className?: string;
}) {
  return (
    <ToggleGroup
      value={[value]}
      onValueChange={(v) => {
        const next = (v as T[])[0];
        if (next) onChange(next);
      }}
      variant="outline"
      size="sm"
      spacing={0}
      aria-label={ariaLabel}
      className={className}
    >
      {options.map((o) => (
        <ToggleGroupItem key={o.value} value={o.value} title={o.title} className={o.className}>
          {o.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

// Leading toolbar shared by both tabs: search + filters left, actions right.
export function Toolbar({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {children}
      {actions != null && <div className="ml-auto flex items-center gap-2">{actions}</div>}
    </div>
  );
}
