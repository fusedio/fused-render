// Plugins section: what is on this machine (Installed) and what the
// marketplaces you have cloned publish that you don't have yet (Discover).
//
// Both lists are ONE LINE PER PLUGIN, through the app's shared `ListRow`. The
// old layout gave each plugin a card with a head row, a sub line and an actions
// row — three lines and ~90px of height to say "enabled, v1.2.3" about
// something you scan twenty of. A row that never wraps says the same thing and
// lets you compare the twenty. Discover's rows expand, because a catalog
// description is the thing you are deciding on and must not be ellipsized into
// nothing; Installed's don't, because `plugins list` has no description to show.
//
// The marketplace column beside the list is a FILTER, not a nav: a plain list
// of rows with counts, no panel background, nothing that could read as a third
// sidebar next to the shell's own. It filters whichever list is showing.
//
// The Installed toggle is optimistic: the flip shows immediately and is rolled
// back if the write fails, because the write is a git commit in the config repo
// and waiting for it made a switch feel like a form submit. There is
// deliberately no reload after a successful toggle — the only thing that
// changed is the flag we already painted, and a refetch here would fight the
// optimistic value.
import { useCallback, useEffect, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { AvailablePlugin, AvailablePlugins, Plugin } from "../api";
import {
  Empty,
  Icon,
  ListRow,
  Pill,
  SKELETON_ROWS,
  SectionToolbar,
  Toggle3,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

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

export default function PluginsSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.plugins.list(), []);
  const { data, error, reload } = useModuleData(load);
  const [tab, setTab] = useState<Tab>("installed");
  const [query, setQuery] = useState("");
  const [marketplace, setMarketplace] = useState(ALL);
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
    cc.plugins.available().then(setAvail, (e: Error) => setAvailError(e.message));
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
      onChanged();
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

  const install = async (p: AvailablePlugin) => {
    setBusy(p.id);
    try {
      const res = await cc.plugins.install(p.id);
      if (!res.ok) {
        toastErr(res.error || "Install failed");
        return;
      }
      toastOk(`Installed ${p.name}`);
      // Both lists moved: the plugin left Discover and joined Installed. And
      // the CLI may have written settings.json's enabledPlugins, which is the
      // config repo — hence onChanged, even though plugins/ itself is ignored.
      reload();
      loadAvail();
      onChanged();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  // Switching lists clears the marketplace filter: the two lists have different
  // marketplaces in them, and a filter naming one that isn't in the new index
  // would show an empty list with no visible reason.
  const pick = (next: Tab) => {
    setTab(next);
    setMarketplace(ALL);
  };

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={SKELETON_ROWS} label="Loading plugins" />;

  const installed = data.plugins.filter((p) => matches(query, p.name, p.id));
  // Discover is only the plugins you do NOT have: what you already installed is
  // the other tab's subject, and showing it twice makes the list longer without
  // making it more useful.
  const discover = (avail?.plugins ?? []).filter(
    (p) => !p.installed && matches(query, p.name, p.id, p.description),
  );
  // The index counts what the SEARCH left, so a marketplace's number is what
  // clicking it will actually show. Kept as one un-narrowed list because the
  // index doesn't care which shape it is counting.
  const shown: { marketplace: string }[] = tab === "installed" ? installed : discover;
  const index = indexOf(shown);
  const inMarketplace = (m: string) => marketplace === ALL || m === marketplace;
  const rowsInstalled = installed.filter((p) => inMarketplace(p.marketplace));
  const rowsDiscover = discover.filter((p) => inMarketplace(p.marketplace));
  const rowCount = tab === "installed" ? rowsInstalled.length : rowsDiscover.length;
  // Counted off the FULL list, not the filtered one: the toolbar summary says
  // what you have, and the index column already says what the filter left.
  const enabledCount = data.plugins.filter((p) => flipped[p.id] ?? p.enabled).length;

  return (
    <>
      <SectionToolbar
        summary={
          tab === "installed"
            ? `${data.plugins.length} installed · ${enabledCount} enabled`
            : avail
              ? `${avail.plugins.filter((p) => !p.installed).length} available from ${
                  indexOf(avail.plugins).length
                } marketplace(s)`
              : "reading marketplace catalogs…"
        }
        // Refetches whichever list is showing — refreshing Installed must not
        // trigger the catalog read that the Discover switch deliberately defers.
        onRefresh={tab === "installed" ? reload : loadAvail}
      >
        <input
          className="field-control cc-scalar"
          type="search"
          aria-label="Filter plugins"
          placeholder="Filter by name, id or description…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="cc-seg" role="tablist" aria-label="Plugin source">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "installed"}
            className={"cc-seg-btn" + (tab === "installed" ? " active" : "")}
            onClick={() => pick("installed")}
            title="Plugins on this machine"
          >
            Installed
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "discover"}
            className={"cc-seg-btn" + (tab === "discover" ? " active" : "")}
            onClick={() => pick("discover")}
            title="Plugins your marketplaces publish that you don't have yet"
          >
            Discover
          </button>
        </div>
      </SectionToolbar>
      <div className="cc-split">
        <nav className="cc-index" aria-label="Filter by marketplace">
          <button
            type="button"
            className={"cc-index-item" + (marketplace === ALL ? " active" : "")}
            aria-pressed={marketplace === ALL}
            onClick={() => setMarketplace(ALL)}
          >
            <span className="cc-index-name">All</span>
            <span className="cc-count">{shown.length}</span>
          </button>
          {index.map((m) => (
            <button
              key={m.name}
              type="button"
              className={"cc-index-item" + (marketplace === m.name ? " active" : "")}
              aria-pressed={marketplace === m.name}
              title={m.name}
              onClick={() => setMarketplace(m.name)}
            >
              <span className="cc-index-name">{m.name}</span>
              <span className="cc-count">{m.n}</span>
            </button>
          ))}
        </nav>
        <div className="cc-rows">
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
            <SkeletonLines rows={SKELETON_ROWS} label="Reading marketplace catalogs" />
          )}
          {tab === "installed" &&
            rowsInstalled.map((p) => {
              const enabled = flipped[p.id] ?? p.enabled;
              return (
                // No chevron here: `plugins list` reads settings.json and
                // installed_plugins.json, neither of which carries a
                // description — everything this row knows is already on the
                // line. The catalog blurb lives on Discover, which is where it
                // is fetched from.
                <ListRow
                  key={p.id}
                  lead={
                    <Toggle3
                      label={`Enable ${p.name}`}
                      value={enabled}
                      onChange={(next) => toggle(p, next)}
                    />
                  }
                  name={p.name}
                  pills={!p.installed ? <Pill>not installed</Pill> : null}
                  secondary={p.id}
                  secondaryTitle={p.id}
                  secondaryMono
                  meta={p.version ? <span className="cc-lrow-meta">v{p.version}</span> : null}
                  actions={
                    <>
                      {p.installed && (
                        <button
                          type="button"
                          className="cc-iconbtn"
                          disabled={busy === p.id}
                          title={busy === p.id ? "Updating…" : "Update this plugin"}
                          aria-label={`Update ${p.name}`}
                          onClick={() => update(p)}
                        >
                          <Icon name="refresh" />
                        </button>
                      )}
                      <button
                        type="button"
                        className="cc-iconbtn"
                        title={`Copy install command — ${p.shareCommand}`}
                        aria-label={`Copy the install command for ${p.name}`}
                        onClick={() => share(p.shareCommand)}
                      >
                        <Icon name="copy" />
                      </button>
                    </>
                  }
                />
              );
            })}
          {tab === "discover" &&
            rowsDiscover.map((p) => (
              <ListRow
                key={p.id}
                name={p.name}
                secondary={p.description}
                secondaryTitle={p.description}
                // Deciding whether to install something is exactly when the
                // marketing copy matters, and a catalog description runs to a
                // paragraph — so the row shows as much as fits and the panel
                // shows all of it, with who wrote it and what it is filed under.
                details={
                  p.description || p.author || p.category || p.keywords.length ? (
                    <>
                      {p.description && <p>{p.description}</p>}
                      <dl className="cc-lrow-dl">
                        {p.author && (
                          <>
                            <dt className="cc-lrow-dt">Author</dt>
                            <dd className="cc-lrow-dd">{p.author}</dd>
                          </>
                        )}
                        {p.category && (
                          <>
                            <dt className="cc-lrow-dt">Category</dt>
                            <dd className="cc-lrow-dd">{p.category}</dd>
                          </>
                        )}
                        {p.keywords.length > 0 && (
                          <>
                            <dt className="cc-lrow-dt">Keywords</dt>
                            <dd className="cc-lrow-dd">{p.keywords.join(", ")}</dd>
                          </>
                        )}
                      </dl>
                    </>
                  ) : null
                }
                meta={
                  <>
                    {p.version && <span className="cc-lrow-meta">v{p.version}</span>}
                    <span className="cc-lrow-meta cc-mono">{p.marketplace}</span>
                  </>
                }
                actions={
                  <button
                    type="button"
                    className="btn"
                    disabled={busy === p.id}
                    title={`claude plugin install ${p.id}`}
                    onClick={() => install(p)}
                  >
                    {busy === p.id ? "Installing…" : "Install"}
                  </button>
                }
              />
            ))}
          {(tab === "installed" || avail) && rowCount === 0 && (
            <Empty>
              {tab === "installed"
                ? data.plugins.length === 0
                  ? "No plugins enabled or installed."
                  : "No plugin matches this filter."
                : (avail?.plugins.length ?? 0) === 0
                  ? "No marketplace catalog to read. Add one on the Marketplaces tab."
                  : "Nothing left to install here."}
            </Empty>
          )}
        </div>
      </div>
    </>
  );
}
