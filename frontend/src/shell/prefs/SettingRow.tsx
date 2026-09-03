// The settings surface's two building blocks (Flow design language: dense
// bordered property rows, label + description left, control right).
//
//   <SettingsSection title description>      — SectionHeading + intro paragraph
//     <SettingRows>                            — the bordered, squared group
//       <SettingRow label description>control</SettingRow>
//       …
//
// Shared by Preferences.tsx and Indexing.tsx (the Indexing tab), which is why
// it is a component and not a class combo repeated in both.
import type { ReactNode } from "react";
import { cn } from "@platform/lib/utils";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { Muted, SectionHeading } from "@platform/ui/flow/Typography";

export function SettingsSection({
  title,
  description,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      <div className="space-y-1">
        <SectionHeading>{title}</SectionHeading>
        {description != null && <Muted>{description}</Muted>}
      </div>
      {children}
    </section>
  );
}

/** The bordered group a run of SettingRows sits in. */
export function SettingRows({ className, children }: { className?: string; children: ReactNode }) {
  return <EntityList className={className}>{children}</EntityList>;
}

/**
 * One property row. `label` gets `htmlFor={controlId}` when the caller supplies
 * the id it put on its control, so clicking the words reaches the switch or
 * select. `note` is the "what is actually in force" line under the label
 * (env-locked prefs) — muted, so the control stays the row's focal point.
 */
export function SettingRow({
  label,
  description,
  note,
  controlId,
  children,
  className,
}: {
  label: ReactNode;
  description?: ReactNode;
  note?: ReactNode;
  controlId?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-slot="setting-row"
      className={cn(
        "flex items-start justify-between gap-6 px-4 py-3 text-sm border-b border-border last:border-b-0",
        className,
      )}
    >
      <div className="min-w-0 flex-1 space-y-0.5">
        <label htmlFor={controlId} className="block text-sm font-medium leading-snug">
          {label}
        </label>
        {description != null && <p className="text-xs text-muted-foreground leading-snug">{description}</p>}
        {note != null && <p className="text-xs text-muted-foreground leading-snug pt-1">{note}</p>}
      </div>
      {children != null && <div className="flex shrink-0 items-center gap-2 pt-px">{children}</div>}
    </div>
  );
}

/** Inline code inside a description — one place for the mono treatment. */
export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn("font-mono text-xs bg-muted px-1 py-px rounded-sm break-all", className)}>{children}</code>;
}
