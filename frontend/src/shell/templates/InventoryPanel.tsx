import { useEffect, useRef, useState } from "react";
import { DownloadIcon, FolderOpenIcon, LockIcon, PlusIcon, SparklesIcon, Trash2Icon, UploadIcon } from "lucide-react";
import {
  deleteTemplate,
  downloadTemplatesExport,
  openTemplateInClaude,
  rawUrl,
} from "@platform/lib/api";
import type { InventoryTemplate, TemplateInventory } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { TemplatesDialog } from "@shell/templates/TemplatesDialog";
import { FilterGroup, KeyPill, Toolbar } from "@shell/templates/chips";
import { SourceSelect } from "@shell/templates/SourceSelect";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Input } from "@platform/shadcn/ui/input";
import { Label } from "@platform/shadcn/ui/label";
import { EntityList, EntityRow } from "@platform/ui/flow/EntityRow";
import { PropertiesPanel, PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { Identifier, Muted, SectionHeading, Tiny } from "@platform/ui/flow/Typography";

type UseFilter = "all" | "used" | "unused";

export function InventoryPanel({
  inventory,
  onImport,
  onNewTemplate,
  onChanged,
}: {
  inventory: TemplateInventory;
  onImport: () => void;
  onNewTemplate: () => void;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // The template whose details fill the right-side properties panel.
  const [focused, setFocused] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [useFilter, setUseFilter] = useState<UseFilter>("all");
  const [deleting, setDeleting] = useState<InventoryTemplate | null>(null);

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  // Open targets the template's absolute folder path from the inventory
  // (works for core under .core-templates AND user), NOT a dir derived from the
  // user registry. Open reuses the file explorer's navigate() (Listing.tsx),
  // which stats the path and shows the directory listing.
  const open = (path: string) => {
    navigate(path);
  };
  const runExport = async (names: string[]) => {
    setError(null);
    try {
      await downloadTemplatesExport(names);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const openClaude = async (name: string) => {
    setError(null);
    try {
      const { url } = await openTemplateInClaude(name);
      window.location.href = url;
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const selectedNames = Array.from(selected);

  const q = query.trim().toLowerCase();
  const matches = (t: InventoryTemplate): boolean => {
    if (sourceFilter !== "all" && t.source !== sourceFilter) return false;
    if (useFilter === "used" && t.usedBy.length === 0) return false;
    if (useFilter === "unused" && t.usedBy.length > 0) return false;
    if (q) {
      const hit =
        t.name.toLowerCase().includes(q) || t.usedBy.some((k) => k.toLowerCase().includes(q));
      if (!hit) return false;
    }
    return true;
  };

  const groups = inventory.sources
    .slice()
    // User (higher precedence) group first, core after.
    .sort((a, b) => b.precedence - a.precedence)
    .map((s) => ({ source: s, items: inventory.templates.filter((t) => t.source === s.id && matches(t)) }))
    .filter((g) => g.items.length > 0);

  const focusedT = focused ? inventory.templates.find((t) => t.name === focused) ?? null : null;
  const focusedSource = focusedT ? inventory.sources.find((s) => s.id === focusedT.source) : undefined;

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4 scrollbar-auto-hide">
        <Toolbar
          actions={
            <>
              <Button variant="outline" size="sm" onClick={onNewTemplate}>
                <PlusIcon data-icon="inline-start" />
                New template
              </Button>
              <Button variant="outline" size="sm" onClick={onImport}>
                <UploadIcon data-icon="inline-start" />
                Import zip
              </Button>
              <Button
                size="sm"
                disabled={selectedNames.length === 0}
                onClick={() => runExport(selectedNames)}
                title={
                  selectedNames.length === 0
                    ? "Select one or more templates to export"
                    : "Export the selected templates as a zip"
                }
              >
                <DownloadIcon data-icon="inline-start" />
                Export selected{selectedNames.length > 0 ? ` (${selectedNames.length})` : ""}
              </Button>
            </>
          }
        >
          <Input
            type="text"
            className="w-64"
            placeholder="Search by name or used-by key…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <FilterGroup<UseFilter>
            ariaLabel="Filter by use"
            value={useFilter}
            onChange={setUseFilter}
            options={[
              { value: "all", label: "All" },
              { value: "used", label: "Used" },
              { value: "unused", label: "Unused" },
            ]}
          />
          <SourceSelect value={sourceFilter} onChange={setSourceFilter} sources={inventory.sources} />
        </Toolbar>
        {error && <ErrorBanner>{error}</ErrorBanner>}
        {groups.length === 0 ? (
          <Muted>No templates match.</Muted>
        ) : (
          groups.map((g) => (
            <section key={g.source.id} className="space-y-2">
              <SectionHeading className="flex items-center gap-2">
                {g.source.label}
                <Tiny className="font-normal normal-case tracking-normal">{g.items.length}</Tiny>
                {!g.source.editable && <LockIcon className="size-3.5" aria-label="Read-only source" />}
              </SectionHeading>
              <EntityList>
                {g.items.map((t) => (
                  <InventoryRow
                    key={t.name}
                    t={t}
                    checked={selected.has(t.name)}
                    focused={focused === t.name}
                    onToggle={() => toggle(t.name)}
                    onFocus={() => setFocused(t.name)}
                  />
                ))}
              </EntityList>
            </section>
          ))
        )}
        {/* Deleting a template folder has no API in the frozen contract — do it
            from the file explorer (Open in explorer / Reveal in Finder). */}
        <Tiny>
          Edit a template's files from the file explorer — this view manages the pool and its
          bindings, not template internals.
        </Tiny>
      </div>

      <PropertiesPanel className="hidden md:block">
        {!focusedT ? (
          <Muted className="text-xs">Select a template to see its details and actions.</Muted>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <TemplateIcon t={focusedT} />
              <span className="truncate text-sm font-medium">{focusedT.name}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {focusedT.shadowsCore && (
                <Badge variant="outline" title="A user folder shadows a core folder of the same name">
                  shadows core
                </Badge>
              )}
              {focusedT.hasCondition && (
                <Badge
                  variant="outline"
                  title="Has a condition.py — this template only shows for files its condition accepts"
                >
                  conditional
                </Badge>
              )}
              {focusedT.hasIcon && <Badge variant="outline">icon.svg</Badge>}
            </div>
            <PropertyList>
              <PropertyRow label="Source">
                <span className="inline-flex items-center gap-1">
                  {focusedSource?.label ?? focusedT.source}
                  {focusedSource && !focusedSource.editable && (
                    <LockIcon className="size-3 text-muted-foreground" aria-label="Read-only" />
                  )}
                </span>
              </PropertyRow>
              <PropertyRow label="Path">
                <Identifier className="break-all whitespace-normal" title={focusedT.path}>
                  {focusedT.path}
                </Identifier>
              </PropertyRow>
              <PropertyRow label="Used by" className="flex-col items-stretch">
                {focusedT.usedBy.length === 0 ? (
                  <Tiny>unused</Tiny>
                ) : (
                  <span className="flex flex-wrap justify-end gap-1">
                    {focusedT.usedBy.map((k) => (
                      <KeyPill key={k}>{k}</KeyPill>
                    ))}
                  </span>
                )}
              </PropertyRow>
            </PropertyList>
            <div className="flex flex-col gap-1.5">
              <Button variant="outline" size="sm" onClick={() => runExport([focusedT.name])} title="Export this template as a zip">
                <DownloadIcon data-icon="inline-start" />
                Export
              </Button>
              <Button variant="outline" size="sm" onClick={() => open(focusedT.path)} title="Open the folder in the file explorer">
                <FolderOpenIcon data-icon="inline-start" />
                Open
              </Button>
              {focusedT.editable && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => openClaude(focusedT.name)}
                  title="Open Claude Code in this template's folder"
                >
                  <SparklesIcon data-icon="inline-start" />
                  Open in Claude
                </Button>
              )}
              {focusedT.editable && (
                <Button variant="destructive" size="sm" onClick={() => setDeleting(focusedT)} title="Delete this user template">
                  <Trash2Icon data-icon="inline-start" />
                  Delete
                </Button>
              )}
            </div>
          </div>
        )}
      </PropertiesPanel>

      {deleting && (
        <DeleteConfirm
          t={deleting}
          // Pass the THROWING export (not runExport, which swallows errors into
          // panel state) so an export-first delete only proceeds when the
          // recovery zip actually downloaded (TV-16/D92 export-first guarantee).
          onExport={() => downloadTemplatesExport([deleting.name])}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            // Drop the deleted name from the multi-select so "Export selected"
            // never carries a name the server no longer has.
            setSelected((prev) => {
              if (!prev.has(deleting.name)) return prev;
              const next = new Set(prev);
              next.delete(deleting.name);
              return next;
            });
            if (focused === deleting.name) setFocused(null);
            setDeleting(null);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

// Confirm dialog for deleting a user template (TV-16 / SPEC §2.8, D109). No
// accent/safe-looking button in a destructive dialog: the recommended path
// ("Export & delete") carries honest danger styling, and the riskier
// "Delete without export" is a text-only danger action anchored far left.
// A checkbox controls the orthogonal registry-bindings cleanup. Core templates
// never reach here (no Delete action rendered for a read-only source).
function DeleteConfirm({
  t,
  onExport,
  onClose,
  onDeleted,
}: {
  t: InventoryTemplate;
  onExport: () => Promise<void>;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [cleanBindings, setCleanBindings] = useState(true);
  const [busy, setBusy] = useState<"export" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const run = async (withExport: boolean) => {
    if (busy !== null) return;
    setError(null);
    try {
      if (withExport) {
        setBusy("export");
        await onExport(); // must succeed before we destroy the folder
      }
      setBusy("delete");
      await deleteTemplate(t.name, cleanBindings);
      onDeleted();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(null);
      }
    }
  };

  return (
    <TemplatesDialog
      title={`Delete “${t.name}”?`}
      onClose={onClose}
      busy={busy !== null}
      className="sm:max-w-[480px]"
      footer={
        <>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive sm:mr-auto"
            onClick={() => void run(false)}
            disabled={busy !== null}
            title="Delete the folder without saving a recovery zip first"
          >
            Delete without export
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy !== null}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" size="sm" onClick={() => void run(true)} disabled={busy !== null}>
            {busy === "export" ? "Exporting…" : busy === "delete" ? "Deleting…" : "Export & delete"}
          </Button>
        </>
      }
    >
      <Muted>
        This removes the user template folder for <Identifier className="text-foreground">{t.name}</Identifier>.
        Without a bindings cleanup, bindings that use it keep the name and show as broken until you
        rebind or remove them.
      </Muted>
      <Label className="cursor-pointer">
        <Checkbox
          checked={cleanBindings}
          disabled={busy !== null}
          onCheckedChange={(checked) => setCleanBindings(checked === true)}
        />
        <span>Remove registry bindings for this template</span>
      </Label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </TemplatesDialog>
  );
}

// The template's icon.svg rendered as a currentColor mask (monochrome,
// theme-tinted — same idiom as ModeSwitcher). Templates without an icon get
// the first-letter placeholder box.
function TemplateIcon({ t }: { t: InventoryTemplate }) {
  if (!t.hasIcon) {
    return (
      <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-md bg-muted text-[10px] font-semibold text-muted-foreground">
        {t.name.charAt(0).toUpperCase()}
      </span>
    );
  }
  const mask = `url("${rawUrl(t.path + "/icon.svg")}")`;
  return (
    <span
      className="inline-block size-4 shrink-0 bg-current [mask-position:center] [mask-repeat:no-repeat] [mask-size:contain]"
      style={{ WebkitMaskImage: mask, maskImage: mask }}
      aria-hidden
    />
  );
}

function InventoryRow({
  t,
  checked,
  focused,
  onToggle,
  onFocus,
}: {
  t: InventoryTemplate;
  checked: boolean;
  focused: boolean;
  onToggle: () => void;
  onFocus: () => void;
}) {
  return (
    // A plain (div) row: the checkbox and the name are two separate controls,
    // so the row itself is not a button (no nested interactive elements).
    <EntityRow
      selected={focused}
      className={cn("py-1.5", checked && "bg-accent/20")}
      leading={
        <span className="flex items-center gap-3">
          <Checkbox checked={checked} onCheckedChange={onToggle} aria-label={"Select " + t.name} />
          <TemplateIcon t={t} />
        </span>
      }
      title={
        <button
          type="button"
          onClick={onFocus}
          aria-pressed={focused}
          className="cursor-pointer rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          title="Show details"
        >
          {t.name}
        </button>
      }
      meta={
        <span className="inline-flex items-center gap-1">
          {t.shadowsCore && (
            <Badge variant="outline" title="A user folder shadows a core folder of the same name">
              shadows core
            </Badge>
          )}
          {t.hasCondition && (
            <Badge variant="outline" title="Has a condition.py — this template only shows for files its condition accepts">
              conditional
            </Badge>
          )}
        </span>
      }
      trailing={
        t.usedBy.length === 0 ? (
          <Tiny>unused</Tiny>
        ) : (
          <span className="flex max-w-96 flex-wrap justify-end gap-1">
            {t.usedBy.slice(0, 6).map((k) => (
              <KeyPill key={k}>{k}</KeyPill>
            ))}
            {t.usedBy.length > 6 && <Tiny>+{t.usedBy.length - 6}</Tiny>}
          </span>
        )
      }
    />
  );
}
