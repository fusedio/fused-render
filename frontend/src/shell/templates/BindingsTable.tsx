import { useState } from "react";
import { PlusIcon, TriangleAlertIcon } from "lucide-react";
import type { RegistryEntry, RegistryResult } from "@platform/lib/api";
import { sourceLabel, type BindFilter } from "@shell/templates/helpers";
import { FilterGroup, TemplateChip, Toolbar, WarnText } from "@shell/templates/chips";
import { SourceSelect } from "@shell/templates/SourceSelect";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@platform/shadcn/ui/table";
import { Identifier, Muted, PageBody } from "@platform/ui/flow/Typography";
import { StatusDot } from "@platform/ui/flow/StatusIcon";

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
    <PageBody className="space-y-4">
      <Toolbar
        actions={
          <Button size="sm" onClick={onAdd}>
            <PlusIcon data-icon="inline-start" />
            Add extension
          </Button>
        }
      >
        <Input
          type="text"
          className="w-64"
          placeholder="Search by key or template…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <FilterGroup<BindFilter>
          ariaLabel="Filter bindings"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "modified", label: "Modified" },
          ]}
        />
        <SourceSelect value={sourceFilter} onChange={setSourceFilter} sources={registry.sources} />
      </Toolbar>
      {rows.length === 0 ? (
        <Muted>No bindings match.</Muted>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="h-8 px-4 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Pattern
                </TableHead>
                <TableHead className="h-8 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Templates
                </TableHead>
                <TableHead className="h-8 px-4 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Source
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((e) => (
                <TableRow
                  key={e.key}
                  onClick={() => onEdit(e)}
                  onKeyDown={(ev) => {
                    if (ev.key === "Enter" || ev.key === " ") {
                      ev.preventDefault();
                      onEdit(e);
                    }
                  }}
                  tabIndex={0}
                  role="button"
                  className="cursor-pointer hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none"
                >
                  <TableCell className="w-48 px-4 py-1.5">
                    <span className="inline-flex items-center gap-2">
                      {/* A user override isn't a lifecycle status — neutral dot. */}
                      {e.overridesCore ? (
                        <StatusDot bucket="neutral" label="User override" className="bg-foreground" />
                      ) : (
                        <span className="inline-block size-1.5 shrink-0" aria-hidden />
                      )}
                      <Identifier className="text-foreground">{e.key}</Identifier>
                    </span>
                  </TableCell>
                  <TableCell className="px-2 py-1.5 whitespace-normal">
                    <div className="flex flex-wrap items-center gap-1">
                      {e.disabled ? (
                        <Badge variant="outline">Disabled</Badge>
                      ) : (
                        e.templates.map((t, i) => (
                          <TemplateChip
                            key={t.name + i}
                            small
                            name={t.name}
                            isDefault={i === 0}
                            broken={!t.exists}
                            title={
                              !t.exists
                                ? "no template folder resolves to this name"
                                : i === 0
                                  ? "default mode"
                                  : undefined
                            }
                          />
                        ))
                      )}
                    </div>
                    {e.error && (
                      <WarnText className="mt-1 inline-flex items-center gap-1" title={e.error}>
                        <TriangleAlertIcon className="size-3.5" /> {e.error}
                      </WarnText>
                    )}
                  </TableCell>
                  <TableCell className="w-32 px-4 py-1.5 text-right">
                    <Badge variant={e.overridesCore ? "secondary" : "outline"}>
                      {sourceLabel(registry, e.resolvedSource)}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </PageBody>
  );
}
