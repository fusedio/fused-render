// Plugins section: what is on this machine (Installed) and what the
// marketplaces you have cloned publish that you don't have yet (Discover).
//
// Both lists are ONE LINE PER PLUGIN, through the app's shared `ListRow`.
// Discover's rows expand, because a catalog description is the thing you are
// deciding on and must not be ellipsized into nothing; Installed's expand to
// what the plugin CONTAINS (read off disk on expand).
//
// The marketplace column beside the list is BOTH the filter AND the
// marketplaces surface (round 2 folded the standalone Marketplaces tab in
// here — it was never worth a tab of its own, just the source list behind
// this one). Each row filters whichever list is showing on click; the (+) at
// the foot opens the add dialog, and each marketplace has a row menu with the
// share/remove actions. It stays plain — no panel background, no border — so
// it reads as a filter, not a third sidebar next to the shell's own.
//
// The Installed toggle is optimistic: the flip shows immediately and is rolled
// back if the write fails, because the write is a git commit in the config repo
// and waiting for it made a switch feel like a form submit. There is
// deliberately no reload after a successful toggle — the only thing that
// changed is the flag we already painted, and a refetch here would fight the
// optimistic value.
import { useCallback, useEffect, useState } from "react";
import { Copy, EllipsisVertical, Plus, RefreshCw } from "lucide-react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { urlForFsPath } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import { Input } from "@platform/shadcn/ui/input";
import { Label } from "@platform/shadcn/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { Identifier, Muted, SectionHeading } from "@platform/ui/flow/Typography";
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
  BARE_BUTTON,
  Empty,
  ErrorNote,
  List,
  ListRow,
  ListSkeleton,
  Meta,
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

const KIND_LABELS: Record<MarketplaceKind, string> = {
  github: "GitHub repository",
  git: "Git URL",
};

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
    <nav className="flex items-center justify-end gap-2 pt-2" aria-label="Discover results pages">
      <Muted aria-current="page" className="text-xs mr-auto">
        {first}–{last} of {total}
      </Muted>
      <Button variant="outline" size="sm" disabled={page === 0} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      <Button variant="outline" size="sm" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        Next
      </Button>
    </nav>
  );
}

// The five kinds of thing a plugin can put in a session, in the order a reader
// cares about them: what it can DO for you first (skills, commands, agents),
// then what it does on its own (hooks, MCP servers). A group with nothing in it
// renders nothing at all.
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
function PluginContentsPanel({ id }: { id: string }) {
  const load = useCallback(() => cc.plugins.contents(id), [id]);
  const { data, error } = useModuleData(load);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <Muted>Reading plugin files…</Muted>;
  if (!data.ok) return <Muted>{data.error || "Could not read this plugin."}</Muted>;

  const groups = GROUPS.map((g) => ({
    ...g,
    items: (data[g.key] as PluginComponent[] | undefined) ?? [],
  })).filter((g) => g.items.length > 0);

  return (
    <div className="space-y-3">
      {data.description && <Muted>{data.description}</Muted>}
      {groups.length === 0 && (
        <Muted>This plugin ships no skills, commands, agents, hooks or MCP servers.</Muted>
      )}
      {/* One grid for the whole panel — every entry's description starts on
          the same left edge instead of wherever its name happened to end. */}
      {groups.map((g) => (
        <section className="space-y-1" key={String(g.key)}>
          <SectionHeading className="text-xs flex items-center gap-2">
            {g.label}
            <span className="font-mono normal-case tracking-normal">{g.items.length}</span>
          </SectionHeading>
          <ul className="grid grid-cols-[minmax(8rem,max-content)_1fr] gap-x-4 gap-y-0.5">
            {g.items.map((it) => (
              // A real anchor, not a click handler: the entry IS a file, so it
              // gets the file's affordances for free — middle-click, cmd-click,
              // a copyable target in the context menu. A new tab because this
              // page holds unsaved-ish state (an open filter, a half-typed
              // marketplace form) that navigating away would lose.
              <li key={it.path + it.name} className="contents">
                <a
                  className="col-span-2 grid grid-cols-subgrid rounded-md px-1 -mx-1 hover:bg-accent/50 text-sm"
                  href={urlForFsPath(it.path)}
                  target="_blank"
                  rel="noopener"
                  title={it.description ? `${it.description}\n\n${it.path}` : it.path}
                >
                  <span className="font-mono text-xs truncate self-center">{it.name}</span>
                  <span className="text-xs text-muted-foreground truncate self-center">{it.description}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {/* Where the files ARE — metadata about the plugin, not one of the
          things it gives you, so it sits below a rule. */}
      {data.root && (
        <a
          className="flex items-baseline gap-3 border-t border-border pt-2 text-xs hover:underline"
          href={urlForFsPath(data.root)}
          target="_blank"
          rel="noopener"
          title={data.root}
        >
          <span className="text-muted-foreground shrink-0">Files</span>
          <Identifier className="truncate">{data.root}</Identifier>
        </a>
      )}
      {/* The server's per-read walk budget ran out (plugins.py's _WalkBudget)
          — an unusually large plugin, not a plugin that genuinely ships this
          little. Said plainly rather than left to quietly under-report. */}
      {data.truncated && (
        <Muted className="text-xs italic">
          This plugin ships more than shown here — the list was cut off to keep this panel fast.
        </Muted>
      )}
    </div>
  );
}

// One row of the marketplace rail: a filter button (name + count) and a fixed
// trailing slot. The slot is the same width whether it holds a menu or not —
// that is what puts the counts in a column.
function RailRow({
  name,
  count,
  active,
  onPick,
  title,
  menu,
}: {
  name: string;
  count: number;
  active: boolean;
  onPick: () => void;
  title?: string;
  menu?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        className={cn(
          // Bare <button>, so it carries the control reset itself — see
          // BARE_BUTTON. Its own px-2/py-1 still wins over the reset's p-0.
          BARE_BUTTON,
          "flex-1 min-w-0 flex items-center justify-between gap-2 rounded-md px-2 py-1 text-sm text-left outline-none hover:bg-accent/50 focus-visible:ring-3 focus-visible:ring-ring/50",
          active && "bg-accent/30 font-medium",
        )}
        aria-pressed={active}
        title={title}
        onClick={onPick}
      >
        <span className="truncate">{name}</span>
        <span className="font-mono text-xs text-muted-foreground tabular-nums">{count}</span>
      </button>
      <span className="size-6 shrink-0 flex items-center justify-center">{menu}</span>
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
  // The rail's own add-a-marketplace dialog — the same form the standalone
  // Marketplaces tab used to open with, now behind a (+) at the foot of the
  // rail instead of a tab of its own.
  const [mktName, setMktName] = useState("");
  const [mktKind, setMktKind] = useState<MarketplaceKind>("github");
  const [mktValue, setMktValue] = useState("");
  const [mktBusy, setMktBusy] = useState(false);
  const [addingMkt, setAddingMkt] = useState(false);
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
    // a later successful refresh would then render the stale error above a
    // perfectly good list.
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
    // showing the same stale error until the panel remounts.
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

  const removeMarketplace = async (name: string) => {
    try {
      const res = await cc.marketplaces.remove(name);
      if (!res.ok) {
        toastErr(res.error || "Remove failed");
        return;
      }
      toastOk("Removed");
      // Removing a marketplace can orphan the current filter — and its plugins
      // left both lists, so the two reads that depend on it both refetch.
      if (marketplace === name) filterTo(ALL);
      onChanged();
      reloadMkt();
      reload();
      if (avail) loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading plugins" />;

  const installed = data.plugins.filter((p) => matches(query, p.name, p.id));
  // Discover is only the plugins you do NOT have: what you already installed is
  // the other tab's subject, and showing it twice makes the list longer without
  // making it more useful.
  const discover = (avail?.plugins ?? []).filter(
    (p) => !p.installed && matches(query, p.name, p.id, p.description),
  );
  // The index counts what the SEARCH left, so a marketplace's number is what
  // clicking it will actually show.
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
  // Discover. The page is clamped at render rather than corrected in an
  // effect: an install removes a row from Discover, and the last page can
  // vanish under a page number that was valid a moment ago.
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
        <Input
          className="w-64 h-7 md:text-xs"
          type="search"
          aria-label="Filter plugins"
          placeholder="Filter by name, id or description…"
          value={query}
          onChange={(e) => search(e.target.value)}
        />
        <Tabs value={tab} onValueChange={(v) => pick(v as Tab)}>
          <TabsList aria-label="Plugin source" className="h-7">
            <TabsTrigger value="installed" title="Plugins on this machine" className="text-xs px-2">
              Installed
            </TabsTrigger>
            <TabsTrigger
              value="discover"
              title="Plugins your marketplaces publish that you don't have yet"
              className="text-xs px-2"
            >
              Discover
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </SectionToolbar>
      <div className="flex gap-6 items-start">
        <nav className="w-52 shrink-0 space-y-0.5 sticky top-0" aria-label="Marketplaces">
          {/* No count beside this label, deliberately: it would count a
              DIFFERENT NOUN (marketplaces) from every number below it
              (plugins), in the same column and the same shape. */}
          <SectionHeading className="text-xs px-2 pb-1">Marketplaces</SectionHeading>
          <RailRow name="All" count={shown.length} active={marketplace === ALL} onPick={() => filterTo(ALL)} />
          {(mktData?.marketplaces ?? []).map((m) => {
            const n = index.find((x) => x.name === m.name)?.n ?? 0;
            return (
              <RailRow
                key={m.name}
                name={m.name}
                count={n}
                active={marketplace === m.name}
                title={m.name}
                onPick={() => filterTo(m.name)}
                // ONE menu per row, in place of two icon buttons and a read-only
                // lock. Remove is always PRESENT and disabled on a read-only
                // marketplace rather than absent: an item you can see and cannot
                // use says "this one is not yours to remove", where a missing
                // item says nothing.
                menu={
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      render={<Button variant="ghost" size="icon-xs" />}
                      aria-label={`Actions for ${m.name}`}
                      title={`Actions for ${m.name}`}
                    >
                      <EllipsisVertical />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-56">
                      <DropdownMenuItem
                        disabled={!m.shareCommand}
                        onClick={() => m.shareCommand && share(m.shareCommand)}
                      >
                        Copy install command
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        variant="destructive"
                        disabled={!m.editable}
                        onClick={() => removeMarketplace(m.name)}
                      >
                        Remove marketplace
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                }
              />
            );
          })}
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start text-muted-foreground mt-1"
            onClick={() => setAddingMkt(true)}
          >
            <Plus />
            Add marketplace
          </Button>
        </nav>
        <div className="flex-1 min-w-0 space-y-3">
          {tab === "discover" && availError && <ErrorNote>{availError}</ErrorNote>}
          {/* A marketplace whose catalog we could not read is stated, not
              swallowed: the list below is short for a reason the user can act
              on (a hand-edited or half-cloned marketplace.json). */}
          {tab === "discover" && avail && avail.skipped.length > 0 && (
            <Muted className="text-xs">
              Could not read the catalog for {avail.skipped.join(", ")}.
            </Muted>
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
                  // expand. A plugin that is recorded but not installed has no
                  // files to read, so it keeps the flat row.
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
                    // The row's icon actions ride in `meta`, BEFORE the version,
                    // so the version keeps a fixed column and the icons to its
                    // left land on one x too.
                    meta={
                      <>
                        <span className="flex items-center gap-0.5 opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100 motion-safe:transition-opacity">
                          {p.installed && (
                            <Button
                              variant="ghost"
                              size="icon-xs"
                              disabled={busy === p.id}
                              title={busy === p.id ? "Updating…" : "Update this plugin"}
                              aria-label={`Update ${p.name}`}
                              onClick={() => update(p)}
                            >
                              <RefreshCw className={cn(busy === p.id && "motion-safe:animate-spin")} />
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            title={`Copy install command — ${p.shareCommand}`}
                            aria-label={`Copy the install command for ${p.name}`}
                            onClick={() => share(p.shareCommand)}
                          >
                            <Copy />
                          </Button>
                        </span>
                        {/* A fixed column, so it must clip rather than push:
                            a plugin pinned to a commit reports the SHA as its
                            version ("v25d22f864ad6"), which is twice as wide
                            as a semver and was running out over the actions
                            beside it. Ellipsized, with the whole string on
                            hover. */}
                        <Meta
                          mono
                          className="w-16 text-right truncate"
                          title={p.version ? `v${p.version}` : undefined}
                        >
                          {p.version ? `v${p.version}` : ""}
                        </Meta>
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
                        {p.description && <p className="text-muted-foreground">{p.description}</p>}
                        <PropertyList className="max-w-md">
                          {p.author && <PropertyRow label="Author">{p.author}</PropertyRow>}
                          {p.category && <PropertyRow label="Category">{p.category}</PropertyRow>}
                          {p.keywords.length > 0 && (
                            <PropertyRow label="Keywords">{p.keywords.join(", ")}</PropertyRow>
                          )}
                        </PropertyList>
                      </>
                    ) : null
                  }
                  meta={
                    <>
                      {p.version && <Meta>v{p.version}</Meta>}
                      <Meta mono>{p.marketplace}</Meta>
                    </>
                  }
                  actions={
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy === p.id}
                      title={`claude plugin install ${p.id}`}
                      onClick={() => install(p)}
                    >
                      {busy === p.id ? "Installing…" : "Install"}
                    </Button>
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
                  <Button size="sm" onClick={() => pick("discover")}>
                    Browse Discover
                  </Button>
                ) : query || marketplace !== ALL ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      search("");
                      filterTo(ALL);
                    }}
                  >
                    Clear the filter
                  </Button>
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
      {/* A dialog, not a panel in the rail: three fields — a name, a kind and a
          source url — do not fit a 208px column. Dismissal is refused while the
          add is in flight, so the form cannot vanish under a pending write. */}
      <Dialog open={addingMkt} onOpenChange={(o) => !o && !mktBusy && setAddingMkt(false)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Add a marketplace</DialogTitle>
            <DialogDescription>Clones the marketplace so its plugins show up in Discover.</DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-[6rem_1fr] items-center gap-x-3 gap-y-3">
            <Label htmlFor="cc-mkt-name">Name</Label>
            <Input
              id="cc-mkt-name"
              placeholder="my-marketplace"
              value={mktName}
              autoFocus
              disabled={mktBusy}
              onChange={(e) => setMktName(e.target.value)}
            />
            <Label htmlFor="cc-mkt-kind">Source kind</Label>
            <Select
              value={mktKind}
              items={KIND_LABELS}
              onValueChange={(v) => v && setMktKind(v as MarketplaceKind)}
              disabled={mktBusy}
            >
              <SelectTrigger id="cc-mkt-kind" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(KIND_LABELS) as MarketplaceKind[]).map((k) => (
                  <SelectItem key={k} value={k}>
                    {KIND_LABELS[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Label htmlFor="cc-mkt-value">{mktKind === "github" ? "Repository" : "URL"}</Label>
            <Input
              id="cc-mkt-value"
              placeholder={mktKind === "github" ? "owner/repo" : "https://example.com/repo.git"}
              value={mktValue}
              disabled={mktBusy}
              onChange={(e) => setMktValue(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" disabled={mktBusy} onClick={() => setAddingMkt(false)}>
              Cancel
            </Button>
            <Button
              disabled={mktBusy || !mktName.trim() || !mktValue.trim()}
              onClick={addMarketplace}
            >
              {mktBusy ? "Adding…" : "Add marketplace"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
