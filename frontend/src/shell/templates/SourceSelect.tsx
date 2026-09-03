// Source filter shared by both tabs: "All sources" + one entry per template
// source. `items` on the root so the trigger shows the label, not the id.
import type { TemplateSource } from "@platform/lib/api";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@platform/shadcn/ui/select";

export function SourceSelect({
  value,
  onChange,
  sources,
}: {
  value: string;
  onChange: (id: string) => void;
  sources: TemplateSource[];
}) {
  const items = [{ value: "all", label: "All sources" }, ...sources.map((s) => ({ value: s.id, label: s.label }))];
  return (
    <Select value={value} onValueChange={(v) => onChange((v as string | null) ?? "all")} items={items}>
      <SelectTrigger size="sm" aria-label="Source" className="min-w-32">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {items.map((it) => (
          <SelectItem key={it.value} value={it.value}>
            {it.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
