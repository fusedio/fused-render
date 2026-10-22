// Preferences page (SPEC §20) — `/preferences`, entered from the sidebar's
// Settings menu. Framed inside the Bots page under Fused Bot (`?embed=1`,
// apps/bots/components/PrefsPanel.tsx).
//
// A LEFT NAV OF GROUPS UNDER THE SIDEBAR'S OWN HEADINGS, A SEARCH BOX OVER
// ALL OF THEM, AND ONE ROW PER SETTING. What the sections and groups are,
// which row is in which, and the words search finds each by all live in
// `settings/registry.ts`; the control behind each row is in the `settings/`
// file named for its group, mapped by `settings/controls.tsx`. This file only
// lays them out.
//
// It replaced five tabs, the first of which ("Render preferences") had grown
// to twenty sections in the order their flags shipped, with no way to search.
// The audit and the design are claude.ai/artifact/NdzDR2rpNpFU3yBgQd5sJk.
// Four controls left the page then: the menu bar's pinned-file view (retired
// for the Dock), Updates' "Check now" (the status bar's Updates chip asks
// it), the Super Bot phone mirror (its setup is in the bot's own settings),
// and the default Claude model (the Claude Config page's settings hold the one
// value every surface reads; a pointer row leads there).
//
// THE URL: `?tab=<group>` picks the group (absent = General). The four ids an
// old tab used — ai, indexing, lan, account — kept their names so existing
// links land; `?tab=render` reads as General. Switching group REPLACES the
// history entry (not pushes), so Back leaves Preferences instead of walking
// back through every tab visited. `?s=<setting id>` opens that
// setting's group and highlights it; `?q=` is the search, kept in the URL with
// replaceState so a reload or a copied link keeps it without each keystroke
// becoming a history entry.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Search,
  SlidersHorizontal,
  Terminal,
  Sparkles,
  ListChecks,
  LayoutGrid,
  FolderSearch,
  ToggleRight,
  Wifi,
  Plug,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { useClaudeConfigStatus } from "@apps/claude_config/available";
import { getPrefs, type Prefs } from "@platform/lib/api";
import { IS_QUERY_EMBED, replaceSearch } from "@platform/lib/router";
import { isBot } from "@platform/lib/flavor";
import { isMac } from "@platform/lib/platform";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import {
  groupOf,
  navSections,
  resolveGroup,
  sectionsOf,
  visibleGroups,
  visibleSettings,
  type GroupId,
  type SettingEntry,
} from "./settings/registry";
import { ClaudeConfigProvider, ClaudePrefRow, isClaudePref, useClaudePrefEntries } from "./settings/claude";
import { searchSettings, tokenize } from "./settings/search";
import { CONTROLS } from "./settings/controls";
import { rowId, SettingsViewContext, type SettingsView } from "./settings/SettingRow";
import { IndexStateProvider } from "./settings/indexing";

const GROUP_ICONS: Record<GroupId, LucideIcon> = {
  general: SlidersHorizontal,
  features: ToggleRight,
  ai: Sparkles,
  "claude-cli": Terminal,
  tasks: ListChecks,
  apps: LayoutGrid,
  indexing: FolderSearch,
  lan: Wifi,
  integrations: Plug,
  advanced: Wrench,
};

const FLASH_MS = 1800;

function urlWith(edit: (p: URLSearchParams) => void): string {
  const params = new URLSearchParams(location.search);
  edit(params);
  const search = params.toString();
  return location.pathname + (search ? "?" + search : "");
}

function Section({
  title,
  entries,
  prefs,
  onChange,
}: {
  title: string;
  entries: readonly SettingEntry[];
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}) {
  const card = (
    <div className="settings-card">
      {entries.map((e) => {
        if (isClaudePref(e)) return <ClaudePrefRow key={e.id} entry={e} />;
        const Control = CONTROLS[e.id as keyof typeof CONTROLS];
        return <Control key={e.id} prefs={prefs} onChange={onChange} />;
      })}
    </div>
  );
  return (
    <section className="settings-section" aria-label={title}>
      <h3 className="settings-section-title">{title}</h3>
      {card}
    </section>
  );
}

// The last answer, kept across the remount every group switch is (see
// `showGroup`), so the next group paints at once instead of behind a skeleton.
// The mount still re-reads; this is only what it shows meanwhile.
let lastPrefs: Prefs | null = null;
// Bumped by every write a control's PUT answers — MODULE-wide, not per mount,
// because a PUT pressed just before a group switch answers into the page that
// was unmounted. A mount's GET that a write has overtaken is older than that
// write, so it is dropped and the write (in `lastPrefs`) painted instead.
let writeGen = 0;

export default function Preferences() {
  // null until the probe answers: the page waits for it (with the prefs GET
  // it waits for anyway) rather than show General for a moment and then flip.
  const claudeConfig = useClaudeConfigStatus();
  return (
    <ClaudeConfigProvider available={claudeConfig === true}>
      <PreferencesPage claudeConfig={claudeConfig} />
    </ClaudeConfigProvider>
  );
}

function PreferencesPage({ claudeConfig }: { claudeConfig: boolean | null }) {
  const [prefs, setPrefsState] = useState<Prefs | null>(lastPrefs);
  // STABLE: it is every control's `onChange`, and an effect that depends on it
  // (the hotkey recorder suspends the global shortcuts for as long as it runs)
  // must not be torn down by an unrelated re-render of this page.
  const setPrefs = useCallback((p: Prefs) => {
    writeGen += 1;
    lastPrefs = p;
    setPrefsState(p);
  }, []);
  const [error, setError] = useState<string | null>(null);
  const params = new URLSearchParams(location.search);
  const [query, setQueryState] = useState(() => params.get("q") ?? "");
  const [flash, setFlash] = useState<string | null>(() => params.get("s"));
  // The selection lives in state, seeded from the URL once: a group switch is
  // an in-place URL sync (`replaceSearch`), which does not bump the nav epoch,
  // so the page keeps its mount, its nav and its fetched data.
  const [tab, setTab] = useState<string | null>(() => params.get("tab"));
  const [target, setTarget] = useState<string | null>(() => params.get("s"));
  const pageRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const bot = isBot();

  useEffect(() => {
    let alive = true;
    const asked = writeGen;
    getPrefs()
      .then((p) => {
        if (!alive) return;
        if (writeGen === asked) lastPrefs = p;
        setPrefsState(lastPrefs);
      })
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const tokens = useMemo(() => tokenize(query), [query]);
  const searching = tokens.length > 0;
  // The catalog is read with the page: the nav's count for Claude Code is
  // every row, so the page waits for the rows the way it waits for the prefs.
  const { rows: claudeRows, ready: claudeReady } = useClaudePrefEntries();
  const loaded = prefs !== null && claudeConfig !== null && claudeReady;
  const entries = useMemo(() => {
    if (!prefs || claudeConfig === null) return [];
    const base: SettingEntry[] = visibleSettings({ prefs, bot, mac: isMac, claudeConfig });
    return [...base, ...claudeRows];
  }, [prefs, bot, claudeConfig, claudeRows]);
  const groups = useMemo(() => visibleGroups(entries), [entries]);
  const sections = useMemo(() => navSections(groups), [groups]);
  const byId = (id: string) => entries.find((e) => e.id === id);

  // Scroll a jumped-to row into view, light it briefly, then let it go.
  useEffect(() => {
    if (!flash || !prefs) return;
    const el = document.getElementById(rowId(flash));
    if (!el) return;
    el.scrollIntoView({ block: "center" });
    const t = window.setTimeout(() => setFlash(null), FLASH_MS);
    return () => window.clearTimeout(t);
  }, [flash, prefs !== null, entries.length]);

  const setQuery = (q: string) => {
    setQueryState(q);
    history.replaceState(history.state, "", urlWith((p) => (q ? p.set("q", q) : p.delete("q"))));
  };

  // Written in place — `?tab=` and `?s=` follow the selection so a reload or a
  // shared link lands on it, and the row a jump names is lit through state.
  const showGroup = (group: GroupId, highlight: string | null = null) => {
    const next = group === "general" ? null : group;
    setTab(next);
    setTarget(highlight);
    setFlash(highlight);
    setQueryState("");
    replaceSearch(
      urlWith((p) => {
        p.delete("q");
        if (highlight) p.set("s", highlight);
        else p.delete("s");
        if (next) p.set("tab", next);
        else p.delete("tab");
      }),
    );
    // A new group starts at its top; a jump scrolls to its row instead.
    if (!highlight) pageRef.current?.scrollTo?.({ top: 0 });
  };

  // `?s=` names a setting: its group wins over `?tab=`.
  const deep = byId(target ?? "");
  const current: GroupId = deep ? deep.group : resolveGroup(tab, groups);

  const hits = useMemo(() => searchSettings(entries, tokens), [entries, tokens]);
  const view: SettingsView = {
    tokens: searching ? tokens : null,
    flashId: flash,
    jump: (id) => {
      const group = byId(id)?.group;
      if (group) showGroup(group, id);
    },
  };

  // Pointer rows count: they are rows a reader can find and follow, and the
  // counts must mean the same thing with and without a search — every row,
  // the Settings group's catalog rows included (the page waits for them).
  const navCount = (g: GroupId) => (searching ? hits : entries).filter((e) => e.group === g).length;

  const head = searching
    ? hits.length
      ? `${hits.length} ${hits.length === 1 ? "setting matches" : "settings match"} “${query.trim()}”`
      : `No settings match “${query.trim()}”`
    : groupOf(current).label;
  const blurb = searching
    ? hits.length
      ? "Change them right here, or follow the path above one to see it in its group."
      : "Try another word — dark, hotkey, token, wifi, queue, model."
    : groupOf(current).blurb;

  return (
    <div className="settings-page" ref={pageRef}>
      <div className="settings-header">
        {/* Framed inside the Bots page's panel the panel's own bar carries
            the title; a second heading here read as a repeat. */}
        {!IS_QUERY_EMBED && <h1 className="settings-title">Preferences</h1>}
        {prefs && (
          <div className="settings-search" role="search">
            <Search aria-hidden="true" className="settings-search-icon" />
            <Input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && query) {
                  // Spent here: framed in the Bots page, a document-level Esc
                  // closes the whole panel (PrefsPanel), and clearing a search
                  // is not a request to leave.
                  e.preventDefault();
                  e.stopPropagation();
                  setQuery("");
                }
              }}
              placeholder={`Search ${entries.length} settings`}
              aria-label="Search settings"
              className="settings-search-input"
              autoComplete="off"
              spellCheck={false}
            />
            {query && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Clear search"
                className="settings-search-clear"
                onClick={() => {
                  setQuery("");
                  searchRef.current?.focus();
                }}
              >
                <X />
              </Button>
            )}
          </div>
        )}
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!loaded && !error && <SkeletonLines rows={4} label="Loading preferences" />}
      {prefs && loaded && (
        <div className="settings-body">
          <nav className="settings-nav" aria-label="Preference groups">
            {sections.map(({ section, groups: inSection }) => (
              <div key={section.id} className="settings-nav-section" role="group" aria-label={section.label}>
                <div className="settings-nav-heading" aria-hidden="true">
                  {section.label}
                </div>
                {inSection.map((g) => {
                  const Icon = GROUP_ICONS[g.id];
                  const active = !searching && g.id === current;
                  const n = navCount(g.id);
                  return (
                    <Button
                      key={g.id}
                      variant="ghost"
                      aria-current={active ? "page" : undefined}
                      className={cn("settings-nav-item", active && "is-active", searching && n === 0 && "is-dim")}
                      onClick={() => showGroup(g.id)}
                    >
                      <Icon data-icon="inline-start" aria-hidden="true" />
                      <span className="settings-nav-label">{g.label}</span>
                      {(searching || n > 0) && <span className="settings-nav-count">{n}</span>}
                    </Button>
                  );
                })}
              </div>
            ))}
          </nav>
          <main className="settings-main">
            <div className="settings-head">
              <div className="settings-head-row">
                <h2>
                  {head}
                  {!searching && groupOf(current).badge && (
                    <Badge variant="outline" className="settings-badge-beta">
                      {groupOf(current).badge}
                    </Badge>
                  )}
                </h2>
              </div>
              <p>{blurb}</p>
            </div>
            <SettingsViewContext.Provider value={view}>
              <IndexStateProvider>
                {searching
                  ? groups
                      .filter((g) => hits.some((e) => e.group === g.id))
                      .map((g) => (
                        <Section
                          key={g.id}
                          title={g.label}
                          entries={hits.filter((e) => e.group === g.id)}
                          prefs={prefs}
                          onChange={setPrefs}
                        />
                      ))
                  : sectionsOf(entries, current).map((s) => (
                      <Section key={s.section} title={s.section} entries={s.entries} prefs={prefs} onChange={setPrefs} />
                    ))}
              </IndexStateProvider>
            </SettingsViewContext.Provider>
          </main>
        </div>
      )}
    </div>
  );
}
