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
// The marketplace column beside the list is BOTH the filter AND the
// marketplaces surface (round 2 folded the standalone Marketplaces tab in
// here — it was never worth a tab of its own, just the source list behind
// this one). Each row still filters whichever list is showing on click; the
// (+) at the foot opens the same add form the old tab had, and each editable
// marketplace gets its own share/remove icon actions. It stays plain — no
// panel background, no border — so it reads as a filter, not a third sidebar
// next to the shell's own.
//
// The Installed toggle is optimistic: the flip shows immediately and is rolled
// back if the write fails, because the write is a git commit in the config repo
// and waiting for it made a switch feel like a form submit. There is
// deliberately no reload after a successful toggle — the only thing that
// changed is the flag we already painted, and a refetch here would fight the
// optimistic value.
import { useCallback, useEffect, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { urlForFsPath } from "@platform/lib/router";
import ContextMenu from "@platform/ui/ContextMenu";
import Modal from "@platform/ui/modal/Modal";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import type {
  AvailablePlugin,
  AvailablePlugins,
  MarketplaceKind,
  Plugin,
  PluginComponent,
  PluginContents,
} from "../api";
import {
  Empty,
  Icon,
  List,
  ListRow,
  ListSkeleton,
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

// The five kinds of thing a plugin can put in a session, in the order a reader
// cares about them: what it can DO for you first (skills, commands, agents),
// then what it does on its own (hooks, MCP servers). A group with nothing in it
// renders nothing at all — an empty "Commands 0" heading is a row of chrome
// saying a plugin does not have something, which is most plugins for most
// kinds.
const GROUPS: { key: keyof PluginContents; label: string }[] = [
  { key: "skills", label: "Skills" },
  { key: "commands", label: "Commands" },
  { key: "agents", label: "Agents" },
  { key: "hooks", label: "Hooks" },
  { key: "mcpServers", label: "MCP servers" },
];

// What one installed plugin actually contributes, under its expanded row.
//
// Mounted ONLY when the row is open (ListRow renders `details` behind `open`),
// which is what makes the per-plugin read affordable: expanding one plugin
// walks one plugin's tree, and a page you never expand reads nothing at all.
// Hence a component with its own fetch rather than data threaded down from the
// section — the mount IS the trigger.
function PluginContentsPanel({ id }: { id: string }) {
  const load = useCallback(() => cc.plugins.contents(id), [id]);
  const { data, error } = useModuleData(load);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <p>Reading plugin files…</p>;
  if (!data.ok) return <p>{data.error || "Could not read this plugin."}</p>;

  const groups = GROUPS.map((g) => ({
    ...g,
    items: (data[g.key] as PluginComponent[] | undefined) ?? [],
  })).filter((g) => g.items.length > 0);

  return (
    <div className="cc-pcontents">
      {data.description && <p className="cc-pblurb">{data.description}</p>}
      {groups.length === 0 && (
        <p className="cc-pblurb">
          This plugin ships no skills, commands, agents, hooks or MCP servers.
        </p>
      )}
      {groups.map((g) => (
        <section className="cc-pgroup" key={String(g.key)}>
          <h4 className="cc-pgroup-title">
            {g.label}
            <span className="cc-count">{g.items.length}</span>
          </h4>
          {/* The panel is one grid and every level down to the anchor passes
              its two tracks along (see .cc-pcontents), which is what puts
              every description on a single left edge. Laid out per-entry, each
              description started wherever its name happened to end — a ragged
              left edge down thirteen rows was the loudest thing in here. */}
          <ul className="cc-pitems">
            {g.items.map((it) => (
              // A real anchor, not a click handler: the entry IS a file, so it
              // gets the file's affordances for free — middle-click,
              // cmd-click, a copyable target in the context menu. A new tab
              // because this page holds unsaved-ish state (an open filter, a
              // half-typed marketplace form) that navigating away would lose.
              <li key={it.path + it.name}>
                <a
                  className="cc-pitem"
                  href={urlForFsPath(it.path)}
                  target="_blank"
                  rel="noopener"
                  title={it.description ? `${it.description}\n\n${it.path}` : it.path}
                >
                  <span className="cc-pitem-name">{it.name}</span>
                  <span className="cc-pitem-desc">{it.description}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {/* Where the files ARE — metadata about the plugin, not one of the
          things it gives you, so it sits below a rule rather than becoming a
          sixth row of whatever group happened to be last. */}
      {data.root && (
        <a
          className="cc-pfiles"
          href={urlForFsPath(data.root)}
          target="_blank"
          rel="noopener"
          title={data.root}
        >
          <span className="cc-pfiles-label">Files</span>
          <span className="cc-pfiles-path">{data.root}</span>
        </a>
      )}
    </div>
  );
}

export default function PluginsSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.plugins.list(), []);
  const { data, error, reload } = useModuleData(load);
  const loadMkt = useCallback(() => cc.marketplaces.list(), []);
  const { data: mktData, reload: reloadMkt } = useModuleData(loadMkt);
  const [tab, setTab] = useState<Tab>("installed");
  const [query, setQuery] = useState("");
  const [marketplace, setMarketplace] = useState(ALL);
  // The rail's own add-a-marketplace disclosure — the same form the standalone
  // Marketplaces tab used to open with, now folded under a (+) at the foot of
  // the rail instead of a tab of its own.
  const [mktName, setMktName] = useState("");
  const [mktKind, setMktKind] = useState<MarketplaceKind>("github");
  const [mktValue, setMktValue] = useState("");
  const [mktBusy, setMktBusy] = useState(false);
  const [addingMkt, setAddingMkt] = useState(false);
  // Which marketplace's row menu is open, and where to hang it. One at a time
  // by construction — the rail has one menu, not one per row.
  const [mktMenu, setMktMenu] = useState<{ name: string; x: number; y: number } | null>(null);
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
      onChanged();
      reloadMkt();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setMktBusy(false);
    }
  };

  // A row's menu. Remove is always PRESENT and disabled on a read-only
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
      onChanged();
      reloadMkt();
      reload();
      if (avail) loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading plugins" />;

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
        <nav className="cc-index" aria-label="Marketplaces">
          {/* No count beside this label, deliberately. It used to carry one,
              and the number it showed counted a DIFFERENT NOUN from every
              number below it — marketplaces here, plugins in each row — while
              sitting in the same right-hand column, in the same shape as the
              clickable row directly beneath it. It read as a row you could
              click, and its value was four rows you can see. */}
          <div className="cc-index-header">Marketplaces</div>
          {/* Structurally the SAME shape as every marketplace row below it —
              a plain div wrapping the .cc-index-filter button — not a
              standalone <button>. It used to BE the outer button, which is
              what gave it browser-default button chrome (a border, centered
              text) no other row in this rail has; it read as a stray control
              from another component. */}
          <div
            className={"cc-index-item cc-index-all" + (marketplace === ALL ? " active" : "")}
          >
            <button
              type="button"
              className="cc-index-filter"
              aria-pressed={marketplace === ALL}
              onClick={() => filterTo(ALL)}
            >
              <span className="cc-index-name">All</span>
              <span className="cc-count">{shown.length}</span>
            </button>
            {/* Empty, and load-bearing: the trail slot is what fixes where a
                row's filter button ends, so a row without one ends 58px
                further right and its count leaves the column — which is
                exactly what "All" did, at the top of the rail where the
                misalignment is most visible. */}
            <div className="cc-index-trail" />
          </div>
          {(mktData?.marketplaces ?? []).map((m) => {
            const n = index.find((x) => x.name === m.name)?.n ?? 0;
            return (
              <div
                key={m.name}
                className={"cc-index-item" + (marketplace === m.name ? " active" : "")}
              >
                <button
                  type="button"
                  className="cc-index-filter"
                  aria-pressed={marketplace === m.name}
                  title={m.name}
                  onClick={() => filterTo(m.name)}
                >
                  <span className="cc-index-name">{m.name}</span>
                  <span className="cc-count">{n}</span>
                </button>
                {/* ONE menu button per row, in place of the two icon buttons
                    and the always-visible read-only lock that used to share
                    this slot. Three glyphs is a lot of a 180px column to spend
                    on a row whose job is a name and a count — and the lock was
                    the worst of them, a permanent fixture saying "you cannot
                    remove this" in a dialect the reader had to already know.
                    That fact now lives where it can be READ: a Remove item
                    that is present and disabled.

                    The slot is fixed-width whether a row's button is showing
                    or not, and that is what puts the counts in a column — the
                    filter button beside it is `flex: 1`, so an equal trail
                    means an equal button and one x for every count. */}
                <div className="cc-index-trail">
                  <button
                    type="button"
                    className={"cc-iconbtn" + (mktMenu?.name === m.name ? " cc-iconbtn-on" : "")}
                    aria-haspopup="menu"
                    aria-expanded={mktMenu?.name === m.name}
                    aria-label={`Actions for ${m.name}`}
                    title={`Actions for ${m.name}`}
                    onPointerDown={(e) => {
                      // This same pointerdown already closed an open menu (it
                      // dismisses on any outside pointerdown), so reopening
                      // here would make the button un-closable.
                      if (mktMenu?.name === m.name) return;
                      const r = e.currentTarget.getBoundingClientRect();
                      setMktMenu({ name: m.name, x: r.left, y: r.bottom + 4 });
                    }}
                  >
                    <Icon name="kebab" />
                  </button>
                </div>
              </div>
            );
          })}
          {/* Opens a dialog, so it is a plain button rather than the
              DisclosureButton this used to be. That control swapped its label
              for "Cancel" while open — and with the rail's borderless styling
              on it, "Cancel" rendered as bare accent-yellow text under the
              marketplace list, which reads as a warning, not a control. */}
          <button
            type="button"
            className="cc-index-add"
            onClick={() => setAddingMkt(true)}
          >
            <Icon name="plus" />
            Add marketplace
          </button>
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
            <ListSkeleton rows={SKELETON_ROWS} label="Reading marketplace catalogs" />
          )}
          {tab === "installed" && rowsInstalled.length > 0 && (
            <List>
              {rowsInstalled.map((p) => {
                const enabled = flipped[p.id] ?? p.enabled;
                return (
                  // The chevron opens what the plugin CONTAINS — its skills,
                  // commands, agents, hooks and MCP servers, read off disk on
                  // expand. Nothing in `plugins list` (settings.json +
                  // installed_plugins.json) knows any of that, which is why
                  // this row had no chevron at all before: everything those two
                  // files hold was already on the line. A plugin that is
                  // recorded but not installed has no files to read, so it
                  // keeps the flat row.
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
                    details={p.installed ? <PluginContentsPanel id={p.id} /> : null}
                    // The row's icon actions ride in `meta`, BEFORE the
                    // version, rather than in `actions` after it. In `actions`
                    // they sat between the version and the chevron, and since
                    // they only appeared on hover, what the row showed at rest
                    // was a 54px hole between two things that belong beside
                    // each other. The version keeps a fixed column (.cc-lrow-
                    // ver) so the icons to its left land on one x too.
                    meta={
                      <>
                        <span className="cc-lrow-inline-actions">
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
                        </span>
                        <span className="cc-lrow-meta cc-lrow-ver">
                          {p.version ? `v${p.version}` : ""}
                        </span>
                      </>
                    }
                  />
                );
              })}
            </List>
          )}
          {tab === "discover" && pagedDiscover.length > 0 && (
            <List>
              {pagedDiscover.map((p) => (
                <ListRow
                  key={p.id}
                  name={p.name}
                  secondary={p.description}
                  secondaryTitle={p.description}
                  // Deciding whether to install something is exactly when the
                  // marketing copy matters, and a catalog description runs to
                  // a paragraph — so the row shows as much as fits and the
                  // panel shows all of it, with who wrote it and what it is
                  // filed under.
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
            </List>
          )}
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
      {/* OUTSIDE .cc-index, and that is load-bearing. The menu is
          position:fixed, but `position: sticky` on the rail makes it a
          stacking context — so a menu rendered inside it has its z-index
          resolved AGAINST ITS SIBLINGS IN THE RAIL, not against the page. The
          rail itself has z-index auto and comes before .cc-rows in the DOM, so
          the plugin list painted straight over the open menu: the toggles
          showed through it, as if the panel were transparent. */}
      {/* A dialog, not a panel in the rail. Three fields — a name, a kind and a
          source url — do not fit a 180px column: the card they were in was a
          bordered surface on a rail that deliberately has none, its select
          barely cleared its own text, and open it stood taller than the
          marketplace list it belonged to. */}
      {addingMkt && (
        <Modal
          title="Add a marketplace"
          onClose={() => setAddingMkt(false)}
          busy={mktBusy}
          // Narrower than the 420px this used to be: with the label beside its
          // control (see .cc-modal-field) three one-line fields no longer need
          // that much width, and 420px next to a 100px label column left the
          // controls with an odd, over-wide measure for "owner/repo".
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
      {mktMenu && (
        <ContextMenu
          x={mktMenu.x}
          y={mktMenu.y}
          items={menuItemsFor(mktMenu.name)}
          onClose={() => setMktMenu(null)}
        />
      )}
    </>
  );
}
