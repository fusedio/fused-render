// Plugins section: what is on this machine (Installed) and what the
// marketplaces you have cloned publish that you don't have yet (Discover).
//
// Both lists are full width, ONE ROW PER PLUGIN through the shared `ListRow`
// (see ../plugin-row): the name over a muted description, the marketplace, the
// version when it is one, an overflow menu, and at the right end the one thing
// the row is for — the enable switch, or Install. A row that never wraps lets
// you compare twenty of them.
//
// The marketplaces are a filter and a place to manage them, and neither earns
// a column of its own: the filter is a select in the list's header, and the
// Marketplaces button opens a dialog with each marketplace's count and
// actions, and the form to add one.
//
// The Installed toggle is optimistic: the flip shows immediately and is rolled
// back if the write fails, because the write is a config file update
// and waiting for it made a switch feel like a form submit. There is
// deliberately no reload after a successful toggle — the only thing that
// changed is the flag we already painted, and a refetch here would fight the
// optimistic value.
import { useCallback, useEffect, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import Modal from "@platform/ui/modal/Modal";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import type { AvailablePlugin, AvailablePlugins, MarketplaceKind, Plugin } from "../api";
import {
  Empty,
  Icon,
  List,
  ListSkeleton,
  PAGE_SIZE,
  Pager,
  SKELETON_ROWS,
  Head,
  RefreshButton,
  Seg,
  plural,
  toastErr,
  toastOk,
  useConfirm,
  useModuleData,
} from "../bits";
import type { HeadedSectionProps } from "../bits";
import { DiscoverPluginRow, InstalledPluginRow } from "../plugin-row";

type Tab = "installed" | "discover";

// The filter's "no marketplace picked" value. A sentinel rather than null so
// the index rows and the state share one type.
const ALL = "";

// Count per marketplace over whatever list is showing, in first-seen order —
// which is alphabetical, because both server actions sort their source.
function indexOf(items: { marketplace: string }[]): { name: string; n: number }[] {
  const out: { name: string; n: number }[] = [];
  for (const p of items) {
    const row = out.find((x) => x.name === p.marketplace);
    if (row) row.n += 1;
    else out.push({ name: p.marketplace, n: 1 });
  }
  return out;
}

function matches(q: string, ...fields: (string | null | undefined)[]): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return fields.some((f) => (f || "").toLowerCase().includes(needle));
}

export default function PluginsSection({ filter = "", title, blurb }: HeadedSectionProps) {
  const { node: modal, ask } = useConfirm();
  const load = useCallback(() => cc.plugins.list(), []);
  const { data, error, reload } = useModuleData(load);
  const loadMkt = useCallback(() => cc.marketplaces.list(), []);
  const { data: mktData, reload: reloadMkt } = useModuleData(loadMkt);
  const [tab, setTab] = useState<Tab>(() =>
    new URLSearchParams(location.search).get("view") === "discover" ? "discover" : "installed",
  );
  const [marketplace, setMarketplace] = useState(ALL);
  // The add-a-marketplace form, in its own dialog.
  const [mktName, setMktName] = useState("");
  const [mktKind, setMktKind] = useState<MarketplaceKind>("github");
  const [mktValue, setMktValue] = useState("");
  const [mktBusy, setMktBusy] = useState(false);
  const [addingMkt, setAddingMkt] = useState(false);
  // The Marketplaces dialog: each marketplace with its count and actions.
  const [managing, setManaging] = useState(false);
  // Local, never in the URL: a URL-held page would re-read every marketplace
  // catalog on each change. Same reasoning as the row-expansion state in
  // ListRow.
  const [page, setPage] = useState(0);
  // id -> optimistically-shown enabled flag, overriding the fetched value.
  const [flipped, setFlipped] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);

  // The catalog read is DEFERRED until you actually ask for Discover: it is the
  // longer of the two lists (a marketplace can publish hundreds of plugins) and
  // most visits to this page never leave Installed. Once fetched it is kept —
  // switching tabs back and forth must not re-read every marketplace.json.
  const [avail, setAvail] = useState<AvailablePlugins | null>(null);
  const [availError, setAvailError] = useState<string | null>(null);
  const loadAvail = useCallback(() => {
    // Both branches write BOTH pieces of state. A sticky `availError` would be
    // worse than the failure it records: the auto-load gate below is
    // `!avail && !availError`, so one failed read disables it permanently, and
    // a later successful refresh would then render the stale ErrorBanner above
    // a perfectly good list.
    cc.plugins.available().then(
      (a) => {
        setAvail(a);
        setAvailError(null);
      },
      (e: Error) => setAvailError(e.message),
    );
  }, []);
  useEffect(() => {
    if (tab === "discover" && !avail && !availError) loadAvail();
  }, [tab, avail, availError, loadAvail]);

  const toggle = async (p: Plugin, next: boolean) => {
    setFlipped((f) => ({ ...f, [p.id]: next }));
    try {
      const res = await cc.plugins.toggle(p.id, next);
      if (!res.ok) throw new Error(res.error || "Toggle failed");
      toastOk(next ? "Enabled" : "Disabled");
    } catch (e) {
      toastErr((e as Error).message);
      setFlipped((f) => {
        const rest = { ...f };
        delete rest[p.id];
        return rest;
      });
    }
  };

  const update = async (p: Plugin) => {
    setBusy(p.id);
    try {
      const res = await cc.plugins.update(p.id);
      if (res.ok) toastOk("Updated");
      else toastErr(res.error || "Update failed");
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const uninstall = async (p: Plugin) => {
    const ok = await ask<boolean>({
      title: `Uninstall "${p.name}"?`,
      note: "Removed through the claude CLI, at user scope.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Uninstall", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    setBusy(p.id);
    try {
      const res = await cc.plugins.uninstall(p.id);
      if (!res.ok) {
        toastErr(res.error || "Uninstall failed");
        return;
      }
      toastOk(`Uninstalled ${p.name}`);
      reload();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const install = async (p: AvailablePlugin) => {
    setBusy(p.id);
    try {
      const res = await cc.plugins.install(p.id);
      if (!res.ok) {
        toastErr(res.error || "Install failed");
        return;
      }
      toastOk(`Installed ${p.name}`);
      // Both lists moved: the plugin left Discover and joined Installed.
      reload();
      loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const filterTo = (next: string) => {
    setMarketplace(next);
    setPage(0);
  };

  // Switching lists clears the marketplace filter: the two lists have different
  // marketplaces in them, and a filter naming one that isn't in the new index
  // would show an empty list with no visible reason.
  const pick = (next: Tab) => {
    setTab(next);
    setMarketplace(ALL);
    setPage(0);
    // Switching lists is also the cheap retry: clearing the error re-opens the
    // auto-load gate, so coming back to Discover tries once more rather than
    // showing the same stale banner until the panel remounts.
    setAvailError(null);
  };

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  const addMarketplace = async () => {
    const n = mktName.trim();
    const v = mktValue.trim();
    if (!n || !v) {
      toastErr("name and value required");
      return;
    }
    setMktBusy(true);
    try {
      const res = await cc.marketplaces.add(n, mktKind, v);
      if (!res.ok) {
        toastErr(res.error || "Add failed");
        return;
      }
      toastOk("Added");
      setMktName("");
      setMktValue("");
      setAddingMkt(false);
      reloadMkt();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setMktBusy(false);
    }
  };

  // A marketplace's actions. Remove is always PRESENT and disabled on a read-only
  // marketplace rather than absent: an item you can see and cannot use says
  // "this one is not yours to remove", where a missing item says nothing and
  // leaves the reader wondering whether they mis-clicked.
  const menuItemsFor = (name: string): MenuEntry[] => {
    const m = (mktData?.marketplaces ?? []).find((x) => x.name === name);
    if (!m) return [];
    return [
      {
        label: "Copy install command",
        disabled: !m.shareCommand,
        onClick: () => m.shareCommand && share(m.shareCommand),
      },
      "separator",
      {
        label: "Remove marketplace",
        danger: true,
        disabled: !m.editable,
        onClick: () => removeMarketplace(name),
      },
    ];
  };

  const removeMarketplace = async (mktName: string) => {
    try {
      const res = await cc.marketplaces.remove(mktName);
      if (!res.ok) {
        toastErr(res.error || "Remove failed");
        return;
      }
      toastOk("Removed");
      // Removing a marketplace can orphan the current filter — and its plugins
      // left both lists, so the two reads that depend on it both refetch.
      if (marketplace === mktName) filterTo(ALL);
      reloadMkt();
      reload();
      if (avail) loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data)
    return (
      <>
        <Head title={title} blurb={blurb} />
        <ListSkeleton rows={SKELETON_ROWS} label="Loading plugins" />
      </>
    );

  const installed = data.plugins.filter((p) => matches(filter, p.name, p.id, p.description));
  // Discover lists every plugin the catalogs publish; the ones you already
  // have say "Installed" where the others have Install.
  const discover = (avail?.plugins ?? []).filter((p) =>
    matches(filter, p.name, p.id, p.description),
  );
  // The select counts what the SEARCH left, so a marketplace's number is what
  // picking it will actually show. Kept as one un-narrowed list because the
  // count doesn't care which shape it is counting.
  const shown: { marketplace: string }[] = tab === "installed" ? installed : discover;
  const index = indexOf(shown);
  const inMarketplace = (m: string) => marketplace === ALL || m === marketplace;
  const rowsInstalled = installed.filter((p) => inMarketplace(p.marketplace));
  const rowsDiscover = discover.filter((p) => inMarketplace(p.marketplace));
  const rowCount = tab === "installed" ? rowsInstalled.length : rowsDiscover.length;
  // Counted off the FULL list, not the filtered one: the summary says what you
  // have, and the select already says what the filter left.
  const enabledCount = data.plugins.filter((p) => flipped[p.id] ?? p.enabled).length;

  // Paging is the LAST step, over the already-filtered list, and only on
  // Discover — Installed is a handful of plugins with a search box over it.
  // The page is clamped at render rather than corrected in an effect: a
  // marketplace removal can shrink the list under a page number that was valid
  // a moment ago.
  const pages = Math.max(1, Math.ceil(rowsDiscover.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const pagedDiscover = rowsDiscover.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  const marketplaces = mktData?.marketplaces ?? [];

  return (
    <>
      {modal}
      <Head
        title={title}
        // Discover's summary counts the SAME set the pager pages over, so the
        // two can never disagree about how many results there are.
        blurb={
          tab === "installed"
            ? `${data.plugins.length} installed · ${enabledCount} enabled`
            : avail
              ? `${rowsDiscover.length} available from ${plural(index.length, "marketplace")}`
              : "reading marketplace catalogs…"
        }
        aside={
          <>
            <select
              className="field-control cc-mkt-select"
              aria-label="Marketplace"
              value={marketplace}
              onChange={(e) => filterTo(e.target.value)}
            >
              <option value={ALL}>All marketplaces ({shown.length})</option>
              {marketplaces.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name} ({index.find((x) => x.name === m.name)?.n ?? 0})
                </option>
              ))}
            </select>
            <button type="button" className="btn" onClick={() => setManaging(true)}>
              Marketplaces
            </button>
            <Seg
              label="Plugin source"
              value={tab}
              onChange={pick}
              options={[
                { id: "installed", label: "Installed", title: "Plugins on this machine" },
                {
                  id: "discover",
                  label: "Discover",
                  title: "Plugins your marketplaces publish",
                },
              ]}
            />
            {/* Refetches whichever list is showing: refreshing Installed must
                not trigger the catalog read that the Discover switch defers. */}
            <RefreshButton onClick={tab === "installed" ? reload : loadAvail} />
          </>
        }
      />
      {tab === "discover" && availError && <ErrorBanner>{availError}</ErrorBanner>}
      {/* A marketplace whose catalog we could not read is stated, not
          swallowed: the list below is short for a reason the user can act
          on (a hand-edited or half-cloned marketplace.json). */}
      {tab === "discover" && avail && avail.skipped.length > 0 && (
        <div className="cc-change">
          Could not read the catalog for {avail.skipped.join(", ")}.
        </div>
      )}
      {tab === "discover" && !avail && !availError && (
        <ListSkeleton rows={SKELETON_ROWS} label="Reading marketplace catalogs" />
      )}
      {tab === "installed" && rowsInstalled.length > 0 && (
        <List>
          {rowsInstalled.map((p) => (
            <InstalledPluginRow
              key={p.id}
              plugin={p}
              enabled={flipped[p.id] ?? p.enabled}
              busy={busy === p.id}
              onToggle={(next) => toggle(p, next)}
              onUpdate={() => update(p)}
              onUninstall={() => uninstall(p)}
              onCopy={share}
            />
          ))}
        </List>
      )}
      {tab === "discover" && pagedDiscover.length > 0 && (
        <List>
          {pagedDiscover.map((p) => (
            <DiscoverPluginRow
              key={p.id}
              plugin={p}
              busy={busy === p.id}
              onInstall={() => install(p)}
              onCopy={share}
            />
          ))}
        </List>
      )}
      {tab === "discover" && (
        <Pager
          page={safePage}
          total={rowsDiscover.length}
          label="Discover results pages"
          onPage={setPage}
        />
      )}
      {/* Each empty state names what is missing and offers the control
          that fixes it — which on this tab is usually the other list. */}
      {(tab === "installed" || avail) && rowCount === 0 && (
        <Empty
          action={
            tab === "installed" && data.plugins.length === 0 ? (
              <button type="button" className="btn btn-primary" onClick={() => pick("discover")}>
                Browse Discover
              </button>
            ) : marketplace !== ALL ? (
              <button type="button" className="btn" onClick={() => filterTo(ALL)}>
                Show every marketplace
              </button>
            ) : null
          }
        >
          {tab === "installed"
            ? data.plugins.length === 0
              ? "No plugins installed. Discover shows what your marketplaces offer."
              : "No plugin matches this filter."
            : (avail?.plugins.length ?? 0) === 0
              ? "No marketplace catalog to read. Add a marketplace and its plugins show up here."
              : "No plugin matches this filter."}
        </Empty>
      )}
      {managing && (
        <Modal
          title="Marketplaces"
          onClose={() => setManaging(false)}
          width={460}
          footer={
            <>
              <button type="button" className="btn btn-secondary" onClick={() => setManaging(false)}>
                Close
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  setManaging(false);
                  setAddingMkt(true);
                }}
              >
                <Icon name="plus" />
                Add marketplace
              </button>
            </>
          }
        >
          {marketplaces.length === 0 && <p>No marketplaces yet.</p>}
          <ul className="cc-mkt-list">
            {marketplaces.map((m) => (
              <li key={m.name} className="cc-mkt-item">
                <span className="cc-mkt-name" title={m.name}>
                  {m.name}
                </span>
                <span className="cc-count">{index.find((x) => x.name === m.name)?.n ?? 0}</span>
                <span className="cc-mkt-actions">
                  {menuItemsFor(m.name).map((it) =>
                    it === "separator" ? null : (
                      <button
                        key={it.label}
                        type="button"
                        className={"btn" + (it.danger ? " btn-danger-text" : "")}
                        disabled={it.disabled}
                        onClick={it.onClick}
                      >
                        {it.label}
                      </button>
                    ),
                  )}
                </span>
              </li>
            ))}
          </ul>
        </Modal>
      )}
      {addingMkt && (
        <Modal
          title="Add a marketplace"
          onClose={() => setAddingMkt(false)}
          busy={mktBusy}
          // With the label beside its control (see .cc-modal-field) three one-line
          // fields do not need more.
          width={360}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={mktBusy}
                onClick={() => setAddingMkt(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={mktBusy || !mktName.trim() || !mktValue.trim()}
                onClick={addMarketplace}
              >
                {mktBusy ? "Adding…" : "Add marketplace"}
              </button>
            </>
          }
        >
          <div className="cc-modal-field">
            <label htmlFor="cc-mkt-name">Name</label>
            <input
              id="cc-mkt-name"
              className="field-control"
              placeholder="my-marketplace"
              value={mktName}
              autoFocus
              disabled={mktBusy}
              onChange={(e) => setMktName(e.target.value)}
            />
          </div>
          <div className="cc-modal-field">
            <label htmlFor="cc-mkt-kind">Source kind</label>
            <select
              id="cc-mkt-kind"
              className="field-control"
              value={mktKind}
              disabled={mktBusy}
              onChange={(e) => setMktKind(e.target.value as MarketplaceKind)}
            >
              <option value="github">GitHub repository</option>
              <option value="git">Git URL</option>
            </select>
          </div>
          <div className="cc-modal-field">
            <label htmlFor="cc-mkt-value">
              {mktKind === "github" ? "Repository" : "URL"}
            </label>
            <input
              id="cc-mkt-value"
              className="field-control"
              placeholder={mktKind === "github" ? "owner/repo" : "https://example.com/repo.git"}
              value={mktValue}
              disabled={mktBusy}
              onChange={(e) => setMktValue(e.target.value)}
            />
          </div>
        </Modal>
      )}
    </>
  );
}
