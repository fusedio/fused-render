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

// Rows per page on Discover. claude-plugins-official alone publishes ~287
// plugins, so an unpaged Discover is a wall you scroll past rather than a list
// you read. Installed is a handful and pages out at one page, which renders no
// pager at all.
const PAGE_SIZE = 25;

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

// The pager, which describes the FILTERED set — the marketplace filter and the
// search have already run by the time it sees a total, so "1–25 of 287" narrows
// with them. Renders nothing at all when everything fits: a lone disabled
// "1 of 1" is chrome that only ever says "there is nothing to page".
function Pager({
  page,
  pages,
  total,
  onPage,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (next: number) => void;
}) {
  if (pages <= 1) return null;
  const first = page * PAGE_SIZE + 1;
  const last = Math.min(total, (page + 1) * PAGE_SIZE);
  return (
    <nav className="cc-pager" aria-label="Discover results pages">
      <span className="cc-summary" aria-current="page">
        {first}–{last} of {total}
      </span>
      <button
        type="button"
        className="btn"
        disabled={page === 0}
        onClick={() => onPage(page - 1)}
      >
        Previous
      </button>
      <button
        type="button"
        className="btn"
        disabled={page >= pages - 1}
        onClick={() => onPage(page + 1)}
      >
        Next
      </button>
    </nav>
  );
}

export default function PluginsSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.plugins.list(), []);
  const { data, error, reload } = useModuleData(load);
  const [tab, setTab] = useState<Tab>("installed");
  const [query, setQuery] = useState("");
  const [marketplace, setMarketplace] = useState(ALL);
  // Local, never in the URL: this panel remounts on every `?cctab=` write, so a
  // URL-held page would be reset by the very navigation meant to preserve it —
  // and it would re-read every marketplace catalog on the way. Same reasoning
  // as the row-expansion state in ListRow.
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

  // Every filter change goes back to page 1. Without this, searching from page
  // 6 lands you on an empty page of a three-page result and the list looks
  // broken — so the three setters that narrow the list are wrapped rather than
  // called raw anywhere.
  const search = (next: string) => {
    setQuery(next);
    setPage(0);
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

  // Paging is the LAST step, over the already-filtered list, and only on
  // Discover — Installed is a handful of plugins with a search box over it.
  // The page is clamped at render rather than corrected in an effect: an
  // install removes a row from Discover, and the last page can vanish under a
  // page number that was valid a moment ago.
  const pages = Math.max(1, Math.ceil(rowsDiscover.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const pagedDiscover = rowsDiscover.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  return (
    <>
      <SectionToolbar
        // Discover's summary counts the SAME set the pager pages over, so the
        // two can never disagree about how many results there are.
        summary={
          tab === "installed"
            ? `${data.plugins.length} installed · ${enabledCount} enabled`
            : avail
              ? `${rowsDiscover.length} available from ${index.length} marketplace(s)`
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
          onChange={(e) => search(e.target.value)}
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
            onClick={() => filterTo(ALL)}
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
              onClick={() => filterTo(m.name)}
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
            pagedDiscover.map((p) => (
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
          {tab === "discover" && (
            <Pager
              page={safePage}
              pages={pages}
              total={rowsDiscover.length}
              onPage={setPage}
            />
          )}
          {/* Each empty state names what is missing and offers the control
              that fixes it — which on this tab is usually the other list. */}
          {(tab === "installed" || avail) && rowCount === 0 && (
            <Empty
              action={
                tab === "installed" && data.plugins.length === 0 ? (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => pick("discover")}
                  >
                    Browse Discover
                  </button>
                ) : query || marketplace !== ALL ? (
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      search("");
                      filterTo(ALL);
                    }}
                  >
                    Clear the filter
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
                  : "Nothing left to install here."}
            </Empty>
          )}
        </div>
      </div>
    </>
  );
}
