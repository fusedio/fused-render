// Templates management view (TEMPLATE_MGMT_SPEC §3) — the `/view/_templates`
// sentinel route, entered from the sidebar footer. Two sections on one page:
//   A. Bindings table — one row per registry key (extension → ordered
//      templates). Edit via the Row editor dialog (pattern builder + template
//      list), disable, or reset a user override to core.
//   B. Library — every resolved template folder grouped by source (core =
//      locked/read-only, user = editable), a properties panel for the focused
//      one with export / open / delete, and a multi-step import wizard.
//
// Template file CONTENTS are not edited here — that is the file explorer's job
// (§4 non-goal). This view manages bindings + the template pool only.
import { useEffect, useRef, useState } from "react";
import { getTemplateInventory, getTemplateRegistry } from "@platform/lib/api";
import type { RegistryEntry, RegistryResult, TemplateInventory } from "@platform/lib/api";
import { BindingsTable } from "@shell/templates/BindingsTable";
import { ImportWizard } from "@shell/templates/ImportWizard";
import { InventoryPanel } from "@shell/templates/InventoryPanel";
import { NewTemplateModal } from "@shell/templates/NewTemplateModal";
import { RowEditorModal } from "@shell/templates/RowEditorModal";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Page, PageHeader } from "@platform/ui/flow/Typography";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";

type PageTab = "bindings" | "library";

export default function Templates() {
  const [inventory, setInventory] = useState<TemplateInventory | null>(null);
  const [registry, setRegistry] = useState<RegistryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ mode: "create" | "edit"; entry: RegistryEntry | null } | null>(
    null,
  );
  const [importing, setImporting] = useState(false);
  const [creatingNew, setCreatingNew] = useState(false);
  const loadSeq = useRef(0);

  // The active tab lives in the URL (`?tab=library`) so browser back/forward
  // moves between tabs. The page is keyed by the nav epoch in App.tsx, so a
  // pushState here remounts this view and it re-derives the tab from the URL —
  // no local tab state to keep in sync. Bindings is the default (clean URL).
  const tab: PageTab = new URLSearchParams(location.search).get("tab") === "library" ? "library" : "bindings";
  const setTab = (next: PageTab) => {
    const params = new URLSearchParams(location.search);
    if (next === "bindings") params.delete("tab");
    else params.set("tab", next);
    const search = params.toString();
    // navigateUrl (not raw pushState) so the nav epoch bumps and App remounts
    // this view to re-derive the tab; back/forward already works via popstate.
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };

  const load = async () => {
    const seq = ++loadSeq.current;
    try {
      const [inv, reg] = await Promise.all([getTemplateInventory(), getTemplateRegistry()]);
      if (seq !== loadSeq.current) return;
      setInventory(inv);
      setRegistry(reg);
      setError(null);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      // Fail closed: a mutation (put/reset/disable/import) already applied
      // server-side before this refetch ran, so keeping the prior tables would
      // present pre-mutation state as current. Drop them and surface the error.
      setInventory(null);
      setRegistry(null);
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loading = !error && (!inventory || !registry);

  return (
    <Page className="flex-1">
      <PageHeader
        title="Templates"
        description="Manage which templates render each file type, browse the template pool, and import or export user templates."
        actions={
          <Tabs value={tab} onValueChange={(v) => setTab(v as PageTab)}>
            <TabsList variant="line">
              <TabsTrigger value="bindings">File bindings</TabsTrigger>
              <TabsTrigger value="library">Library</TabsTrigger>
            </TabsList>
          </Tabs>
        }
      />
      {error && (
        <div className="px-6 py-4">
          <ErrorBanner>{error}</ErrorBanner>
        </div>
      )}
      {loading && (
        <div className="space-y-2 px-6 py-4" role="status" aria-busy="true" aria-label="Loading templates">
          {[72, 54, 63, 48, 66].map((w, i) => (
            <Skeleton key={i} className="h-4" style={{ width: `${w}%` }} />
          ))}
        </div>
      )}
      {inventory && registry && tab === "bindings" && (
        <BindingsTable
          registry={registry}
          onEdit={(entry) => setEditor({ mode: "edit", entry })}
          onAdd={() => setEditor({ mode: "create", entry: null })}
        />
      )}
      {inventory && registry && tab === "library" && (
        <InventoryPanel
          inventory={inventory}
          onImport={() => setImporting(true)}
          onNewTemplate={() => setCreatingNew(true)}
          onChanged={load}
        />
      )}

      {editor && inventory && registry && (
        <RowEditorModal
          mode={editor.mode}
          entry={editor.entry}
          inventory={inventory}
          registry={registry}
          onClose={() => setEditor(null)}
          onSaved={load}
        />
      )}
      {importing && <ImportWizard onClose={() => setImporting(false)} onImported={load} />}
      {creatingNew && (
        <NewTemplateModal
          // Literal-extension keys already in the registry (simple + compound;
          // wildcard/directory shapes aren't plain extensions), offered as
          // one-click suggestions. registry can be null here (e.g. a prior
          // load failed) — an empty suggestion list is fine, free typing still
          // works.
          knownExtensions={
            registry
              ? Array.from(
                  new Set(
                    registry.entries
                      .filter((e) => e.keyKind === "simple" || e.keyKind === "compound")
                      .map((e) => e.key),
                  ),
                ).sort()
              : []
          }
          onClose={() => setCreatingNew(false)}
          // Mounted at this level (not inside InventoryPanel, D-precedent:
          // ImportWizard above) so a failed onCreated refresh — which fail-
          // closes inventory/registry to null and would otherwise unmount
          // InventoryPanel — can't take the dialog down with it. The success
          // screen and "Open in Claude" CTA stay visible regardless.
          onCreated={load}
        />
      )}
    </Page>
  );
}
