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
// The Claude Code group (settings/claude.tsx) is the former Claude Config
// panel: its sections are BLOCKS — a card each, not a row — and the rows of
// settings.json come from a catalog fetched at mount, spliced in after the
// registry's catalog row so the nav count, the search and the page agree.
//
// It replaced five tabs, the first of which ("Render preferences") had grown
// to twenty sections in the order their flags shipped, with no way to search.
// The audit and the design are claude.ai/artifact/NdzDR2rpNpFU3yBgQd5sJk.
// Four controls left the page then: the menu bar's pinned-file view (retired
// for the Dock), Updates' "Check now" (the status bar's Updates chip asks
// it), the Super Bot phone mirror (its setup is in the bot's own settings),
// and the default Claude model (Claude Config's Model is the one value every
// surface reads; a pointer row leads there).
//
// THE URL: `?tab=<group>` picks the group (absent = General). The four ids an
// old tab used — ai, indexing, lan, account — kept their names so existing
// links land; `?tab=render` reads as General. Switching group REPLACES the
// history entry (not pushes), so Back leaves Preferences instead of walking
// back through every tab visited. `?s=<setting id>` opens that
// setting's group and highlights it; `?q=` is the search, kept in the URL with
// replaceState so a reload or a copied link keeps it without each keystroke
// becoming a history entry.
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight,
  Search,
  SlidersHorizontal,
  Sparkles,
  SquareTerminal,
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
import { IS_QUERY_EMBED, navigateUrl } from "@platform/lib/router";
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
import {
  ClaudeConfigProvider,
  ClaudeGitChip,
  ClaudePrefRow,
  isClaudePref,
  useClaudePrefEntries,
  useClaudeSectionLayout,
} from "./settings/claude";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@platform/shadcn/ui/collapsible";
import { searchSettings, tokenize } from "./settings/search";
import { CONTROLS } from "./settings/controls";
import { rowId, SettingsViewContext, type SettingsView } from "./settings/SettingRow";
import { IndexStateProvider } from "./settings/indexing";

const GROUP_ICONS: Record<GroupId, LucideIcon> = {
  general: SlidersHorizontal,
  features: ToggleRight,
  ai: Sparkles,
  claude: SquareTerminal,
  tasks: ListChecks,
  apps: LayoutGrid,
  indexing: FolderSearch,
  lan: Wifi,
  integrations: Plug,
  advanced: Wrench,
};

const FLASH_MS = 1800;

/** The Claude Code group's sections in the order this visit opens them in. */
function claudeSections<T extends { section: string }>(
  sections: T[],
  layout: { order: (names: readonly string[]) => string[] },
): T[] {
  const order = layout.order(sections.map((s) => s.section));
  return [...sections].sort((a, b) => order.indexOf(a.section) - order.indexOf(b.section));
}

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
  collapsible,
}: {
  title: string;
  entries: readonly SettingEntry[];
  prefs: Prefs;
  onChange: (p: Prefs) => void;
  /** An accordion (the Claude Code group): the title is the toggle. */
  collapsible?: { open: boolean; onToggle: () => void };
}) {
  // A block is its section's only entry: the card is the block, captioned
  // with the file it edits. In search results (SettingsViewContext.tokens)
  // the block's control is a pointer row and the card is an ordinary one.
  const block = entries.find((e) => e.block) ?? null;
  const searching = useContext(SettingsViewContext).tokens !== null;
  const card = (
    <div className={cn("settings-card", block && !searching && "settings-card-block")}>
      {block && !searching && (
        <div className="settings-block-caption">
          <code>{block.description}</code>
          {block.readOnly && <Badge variant="secondary">read-only</Badge>}
        </div>
      )}
      {entries.map((e) => {
        if (isClaudePref(e)) return <ClaudePrefRow key={e.id} entry={e} />;
        const Control = CONTROLS[e.id as keyof typeof CONTROLS];
        return <Control key={e.id} prefs={prefs} onChange={onChange} />;
      })}
    </div>
  );
  if (!collapsible) {
    return (
      <section className="settings-section" aria-label={title}>
        <h3 className="settings-section-title">{title}</h3>
        {card}
      </section>
    );
  }
  return (
    <Collapsible
      open={collapsible.open}
      onOpenChange={collapsible.onToggle}
      render={<section className="settings-section settings-section-collapsible" aria-label={title} />}
    >
      <h3 className="settings-section-title">
        <CollapsibleTrigger className="settings-section-toggle">
          <ChevronRight aria-hidden="true" className="settings-section-chevron" />
          {title}
          {!block && <span className="settings-section-count">{entries.length}</span>}
        </CollapsibleTrigger>
      </h3>
      <CollapsibleContent>{card}</CollapsibleContent>
    </Collapsible>
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
  // The Claude Code group's store wraps the page: the catalog rows it holds
  // are part of the entry list (search included), and the git chip in the
  // group's head reads the same epoch its blocks bump.
  // null until the probe answers: the page waits for it (with the prefs GET
  // it waits for anyway) rather than resolve `?tab=claude` to General for a
  // moment and then flip — every job and notification link into the group
  // lands on a cold cache.
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
  const wantsClaude = params.get("tab") === "claude" || (params.get("s") ?? "").startsWith("claude-");
  const claudeRows = useClaudePrefEntries(claudeConfig === true && (searching || wantsClaude));
  const entries = useMemo(() => {
    if (!prefs || claudeConfig === null) return [];
    const base: SettingEntry[] = visibleSettings({ prefs, bot, mac: isMac, claudeConfig });
    // The settings.json rows follow the catalog row that introduces them.
    const at = base.findIndex((e) => e.id === "claude-catalog");
    return at === -1 ? base : [...base.slice(0, at + 1), ...claudeRows, ...base.slice(at + 1)];
  }, [prefs, bot, claudeConfig, claudeRows]);
  const groups = useMemo(() => visibleGroups(entries), [entries]);
  const sections = useMemo(() => navSections(groups), [groups]);
  const byId = (id: string) => entries.find((e) => e.id === id);
  const claudeLayout = useClaudeSectionLayout();

  // Scroll a jumped-to row into view, light it briefly, then let it go — once
  // the row IS on the page: a settings.json row is not until its catalog has
  // loaded, and a timer started at mount would have cleared the highlight
  // before then. Re-run as the entry list grows for the same reason. (The
  // blocks above a row fetch their own lists and grow after this scroll; a
  // deep link into a late row can land a little short. Accepted.)
  useEffect(() => {
    if (!flash || !prefs) return;
    // A row inside a closed Claude section: open it, then land on the row.
    const target = byId(flash);
    if (target?.group === "claude" && !claudeLayout.isOpen(target.section)) {
      claudeLayout.ensureOpen(target.section);
      return;
    }
    const el = document.getElementById(rowId(flash));
    if (!el) return;
    el.scrollIntoView({ block: "center" });
    const t = window.setTimeout(() => setFlash(null), FLASH_MS);
    return () => window.clearTimeout(t);
  }, [flash, prefs !== null, entries.length, claudeLayout]);

  const setQuery = (q: string) => {
    setQueryState(q);
    history.replaceState(history.state, "", urlWith((p) => (q ? p.set("q", q) : p.delete("q"))));
  };

  // A navigation, so App.tsx remounts this page (it is keyed on the nav
  // epoch): what the next mount should highlight travels in `?s=`, not in
  // state that is about to be thrown away.
  const showGroup = (group: GroupId, highlight: string | null = null) => {
    navigateUrl(
      urlWith((p) => {
        p.delete("q");
        if (highlight) p.set("s", highlight);
        else p.delete("s");
        if (group === "general") p.delete("tab");
        else p.set("tab", group);
      }),
      { replace: true },
    );
  };

  // `?s=` names a setting: its group wins over `?tab=`. A settings.json row
  // (`claude-pref:…`) is in `entries` only once its catalog has loaded; until
  // then `?tab=claude` — which every link to one of them also carries — is
  // what decides.
  const deep = byId(params.get("s") ?? "");
  const current: GroupId = deep ? deep.group : resolveGroup(params.get("tab"), groups);

  const hits = useMemo(() => searchSettings(entries, tokens), [entries, tokens]);
  const view: SettingsView = {
    tokens: searching ? tokens : null,
    flashId: flash,
    jump: (id) => {
      // A settings.json row is not in `entries` until its catalog has loaded —
      // from AI & agents the store is still asleep — but its group is known,
      // and the mount that follows highlights the row once it exists (the
      // flash effect waits for it).
      const group = byId(id)?.group ?? (isClaudePref({ id } as SettingEntry) ? "claude" : undefined);
      if (group) showGroup(group, id);
    },
  };

  // Pointer rows count: they are rows a reader can find and follow. Outside a
  // search the count is the registry's rows only — the Claude Code group's
  // settings.json rows arrive with their catalog, and a number that read 7
  // and then 83 was a number nobody could trust (Akshil, 2026-10-20). A
  // search counts every hit, catalog rows included: that is what it found.
  const navCount = (g: GroupId) =>
    (searching ? hits : entries.filter((e) => !isClaudePref(e))).filter((e) => e.group === g).length;

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
    <div className="settings-page">
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
      {!(prefs && claudeConfig !== null) && !error && <SkeletonLines rows={4} label="Loading preferences" />}
      {prefs && claudeConfig !== null && (
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
                {!searching && current === "claude" && (
                  <ClaudeGitChip onOpen={() => claudeLayout.ensureOpen("History")} />
                )}
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
                  : current === "claude"
                    ? claudeSections(sectionsOf(entries, current), claudeLayout).map((s) => (
                        <Section
                          key={s.section}
                          title={s.section}
                          entries={s.entries}
                          prefs={prefs}
                          onChange={setPrefs}
                          collapsible={{ open: claudeLayout.isOpen(s.section), onToggle: () => claudeLayout.toggle(s.section) }}
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
