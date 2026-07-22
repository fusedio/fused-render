// Templates management view (TEMPLATE_MGMT_SPEC §3) — the `/view/_templates`
// sentinel route, entered from the sidebar footer. Two sections on one page:
//   A. Bindings table — one row per registry key (extension → ordered
//      templates). Edit via the Row editor modal (pattern builder + template
//      list), disable, or reset a user override to core.
//   B. Inventory panel — every resolved template folder grouped by source
//      (core = locked/read-only, user = editable), with export / reveal / open
//      and a multi-step import wizard.
//
// Template file CONTENTS are not edited here — that is the file explorer's job
// (§4 non-goal). This view manages bindings + the template pool only.
import { useEffect, useRef, useState } from "react";
import { getTemplateInventory, getTemplateRegistry } from "../lib/api";
import type { RegistryEntry, RegistryResult, TemplateInventory } from "../lib/api";
import { BindingsTable } from "./templates/BindingsTable";
import { ImportWizard } from "./templates/ImportWizard";
import { InventoryPanel } from "./templates/InventoryPanel";
import { NewTemplateModal } from "./templates/NewTemplateModal";
import { RowEditorModal } from "./templates/RowEditorModal";
import { navigateUrl } from "../lib/router";
import { ErrorBanner } from "../components/ErrorBanner";

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

  return (
    <div className="templates-page">
      <div className="templates-header">
        <h1>Templates</h1>
        <p className="templates-subtitle">
          Manage which templates render each file type, browse the template pool, and import or
          export user templates.
        </p>
      </div>
      <div className="templates-tabs">
        <button
          type="button"
          className={"templates-tab" + (tab === "bindings" ? " active" : "")}
          onClick={() => setTab("bindings")}
        >
          File bindings
        </button>
        <button
          type="button"
          className={"templates-tab" + (tab === "library" ? " active" : "")}
          onClick={() => setTab("library")}
        >
          Library
        </button>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!error && (!inventory || !registry) && <div className="deploy-muted">Loading…</div>}
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
      {importing && (
        <ImportWizard onClose={() => setImporting(false)} onImported={load} />
      )}
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
          // InventoryPanel — can't take the modal down with it. The success
          // screen and "Open in Claude" CTA stay visible regardless.
          onCreated={load}
        />
      )}
    </div>
  );
}
