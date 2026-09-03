// "+ Add template" popover for the row editor: every inventory template not
// already chosen, grouped by source, plus the shell sentinels. A shadcn
// Popover nested inside the editor dialog — base-ui scopes Escape to the
// innermost popup, so Esc closes the picker, not the dialog.
import type { ReactNode } from "react";
import type { RegistryResult, TemplateInventory } from "@platform/lib/api";
import { sourceLabel } from "@shell/templates/helpers";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { Muted, SectionHeading } from "@platform/ui/flow/Typography";

export function TemplatePicker({
  inventory,
  registry,
  exclude,
  open,
  onOpenChange,
  onPick,
  trigger,
}: {
  inventory: TemplateInventory;
  registry: RegistryResult;
  exclude: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (name: string) => void;
  trigger: ReactNode;
}) {
  const excludeSet = new Set(exclude);
  const groups = inventory.sources
    .slice()
    // User (higher precedence) group first, core after — matches the Library tab.
    .sort((a, b) => b.precedence - a.precedence)
    .map((s) => ({
      source: s,
      items: inventory.templates.filter((t) => t.source === s.id && !excludeSet.has(t.name)),
    }))
    .filter((g) => g.items.length > 0);
  // Shell sentinels (PT-12) are valid registry names but back no template
  // folder, so they aren't in the inventory — offer them explicitly so a
  // removed `_render`/`_listing` can be added back from the UI.
  const sentinels = ["_render", "_listing"].filter((n) => !excludeSet.has(n));
  const empty = groups.length === 0 && sentinels.length === 0;

  const cell = (name: string, title?: string, hasIcon?: boolean) => (
    <button
      key={name}
      type="button"
      role="option"
      aria-selected={false}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left font-mono text-xs hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
      onClick={() => onPick(name)}
      title={title}
    >
      {hasIcon && <span className="size-1.5 rounded-full bg-foreground" title="has icon.svg" aria-hidden />}
      <span className="truncate">{name}</span>
    </button>
  );

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger render={<span className="inline-flex" />}>{trigger}</PopoverTrigger>
      <PopoverContent align="start" aria-label="Add template" className="w-72 max-h-80 overflow-y-auto p-2">
        {empty && <Muted>No more templates to add.</Muted>}
        {groups.map((g) => (
          <div key={g.source.id} role="listbox" aria-label={sourceLabel(registry, g.source.id)} className="mb-2 last:mb-0">
            <SectionHeading className="px-2 py-1 text-xs">{sourceLabel(registry, g.source.id)}</SectionHeading>
            {g.items.map((t) => cell(t.name, undefined, t.hasIcon))}
          </div>
        ))}
        {sentinels.length > 0 && (
          <div role="listbox" aria-label="Special modes">
            <SectionHeading className="px-2 py-1 text-xs">Special modes</SectionHeading>
            {sentinels.map((name) => cell(name, "Shell built-in mode (no template folder)"))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
