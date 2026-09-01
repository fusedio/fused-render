import { useState } from "react";
import type { RegistryEntry, RegistryResult } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@platform/shadcn/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { sourceLabel, type BindFilter } from "@shell/templates/helpers";

export function BindingsTable({
  registry,
  onEdit,
  onAdd,
}: {
  registry: RegistryResult;
  onEdit: (entry: RegistryEntry) => void;
  onAdd: () => void;
}) {
  const [filter, setFilter] = useState<BindFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [query, setQuery] = useState("");

  const q = query.trim().toLowerCase();
  const rows = registry.entries
    .filter((e) => {
      if (filter === "modified" && !e.overridesCore) return false;
      if (sourceFilter !== "all" && e.resolvedSource !== sourceFilter) return false;
      if (q) {
        const hit =
          e.key.toLowerCase().includes(q) ||
          e.templates.some((t) => t.name.toLowerCase().includes(q));
        if (!hit) return false;
      }
      return true;
    })
    // User-overridden bindings first, then core; key order preserved within
    // each group (stable sort).
    .sort((a, b) => Number(b.overridesCore) - Number(a.overridesCore));

  return (
    <section className="templates-tabpanel">
      <div className="templates-toolbar">
        <Input
          type="text"
          className="max-w-xs"
          placeholder="Search by key or template…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <ToggleGroup
          variant="outline"
          value={[filter]}
          onValueChange={(groupValue) => {
            const next = (groupValue as BindFilter[])[0];
            if (next) setFilter(next);
          }}
        >
          <ToggleGroupItem value="all">All</ToggleGroupItem>
          <ToggleGroupItem value="modified">Modified</ToggleGroupItem>
        </ToggleGroup>
        <Select value={sourceFilter} onValueChange={(v) => setSourceFilter(v as string)}>
          <SelectTrigger>
            <SelectValue>
              {(v: string) =>
                v === "all"
                  ? "All sources"
                  : registry.sources.find((s) => s.id === v)?.label ?? v
              }
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All sources</SelectItem>
            {registry.sources.map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button type="button" className="templates-toolbar-push" onClick={onAdd}>
          + Add extension
        </Button>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-muted-foreground">No bindings match.</div>
      ) : (
        <Table className="templates-table">
          <TableHeader>
            <TableRow>
              <TableHead>Pattern</TableHead>
              <TableHead>Templates</TableHead>
              <TableHead>Source</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((e) => (
              <TableRow key={e.key} onClick={() => onEdit(e)} className="templates-row">
                <TableCell className="templates-col-pattern">
                  {e.overridesCore && <span className="templates-dot" title="User override" />}
                  <code className="templates-key-pill">{e.key}</code>
                </TableCell>
                <TableCell className="templates-col-templates">
                  {e.disabled ? (
                    <span className="templates-pill">Disabled</span>
                  ) : (
                    e.templates.map((t, i) => (
                      <span
                        key={t.name + i}
                        className={
                          "templates-chip small" +
                          (i === 0 ? " default" : "") +
                          (t.exists ? "" : " broken")
                        }
                        title={
                          !t.exists
                            ? "no template folder resolves to this name"
                            : i === 0
                              ? "default mode"
                              : undefined
                        }
                      >
                        {i === 0 && <span className="templates-chip-badge">default</span>}
                        {t.name}
                      </span>
                    ))
                  )}
                  {e.error && (
                    <div className="templates-key-error" title={e.error}>
                      ⚠ {e.error}
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <span className={"registry-source " + (e.overridesCore ? "user" : "")}>
                    {sourceLabel(registry, e.resolvedSource)}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}
