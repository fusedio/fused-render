// The Claude Code group of Preferences: Claude Code's own configuration under
// ~/.claude, drawn with the page's own rows and cards. Until 2026-10-20 this
// was the Claude Config panel, a page of its own at /claude-config with a tab
// strip (Plugins, Memory, Skills, Statusline, MCP, Preferences, History) and
// a settings-page vocabulary of its own; the sections are the same components
// (apps/claude_config/sections), each now a block in a card of this group,
// one long page in the panel's order.
//
// TWO THINGS LIVE HERE AND NOWHERE ELSE:
//
//   * the settings.json rows. The catalog (GET preferences) is fetched once
//     per mount and each of its entries becomes a row — `claude-pref:<key>` —
//     bucketed into the catalog's own groups ("Model & reasoning", …), so a
//     search for "model" or "effort" lands on the row itself, the way it lands
//     on any registry row (Akshil: "this is the global search, search over
//     everything"). The registry cannot list them: they are data the server
//     ships, refreshed from Anthropic's docs by the Refresh catalog button.
//
//   * the git epoch. Every block can dirty the repo (`onChanged`), the History
//     block commits it (`onCommitted`), and the chip beside the group's title
//     carries the dirty state — the one fact that matters while a reader is
//     looking at some other block. A commit rewrites state no block could have
//     predicted, so each remounts on the commit epoch and refetches.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Clock, RefreshCw, Undo2 } from "lucide-react";
import * as cc from "@apps/claude_config/api";
import type { PrefEntry, PreferencesGet } from "@apps/claude_config/api";
import { toastErr, toastOk, useGitStatus, type GitStatusState } from "@apps/claude_config/bits";
import { optionLabel, storedOptionLabel } from "@apps/claude_config/pref-option-label";
import HistorySection from "@apps/claude_config/sections/HistorySection";
import McpSection from "@apps/claude_config/sections/McpSection";
import MemorySection from "@apps/claude_config/sections/MemorySection";
import PluginsSection from "@apps/claude_config/sections/PluginsSection";
import SkillsSection from "@apps/claude_config/sections/SkillsSection";
import StatuslineSection from "@apps/claude_config/sections/StatuslineSection";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { cn } from "@platform/lib/utils";
import { PointerRow } from "./ai";
import type { SettingEntry, SettingId } from "./registry";
import { labelId, rowId, SettingRow, SettingSelect, SettingsViewContext } from "./SettingRow";

// -- the store ---------------------------------------------------------------

interface ClaudeStore {
  available: boolean;
  /** Bumped by every edit; the git chip re-reads on it. */
  badgeEpoch: number;
  /** Bumped by a commit; every block remounts on it. */
  sectionEpoch: number;
  onChanged: () => void;
  onCommitted: () => void;
  git: GitStatusState;
  catalog: PreferencesGet | null;
  catalogError: string | null;
  /** The catalog GET has answered (rows or an error): the page can count. */
  catalogReady: boolean;
  reloadCatalog: () => void;
  patch: (key: string, value: unknown) => Promise<void>;
  refreshCatalog: () => Promise<void>;
  refreshing: boolean;
  /** Start the reads. The provider wraps the whole Preferences page, and a
   *  reader on General never needs the git status or the catalog; the page
   *  calls this once the Claude Code group is shown or a search starts. */
  wake: () => void;
  /** Note an edit in a section, for the order the next visit opens in. */
  touch: (section: string) => void;
}

const IDLE_GIT: GitStatusState = { status: null, failed: false, recheck: () => undefined };

const ClaudeContext = createContext<ClaudeStore>({
  available: false,
  badgeEpoch: 0,
  sectionEpoch: 0,
  onChanged: () => undefined,
  onCommitted: () => undefined,
  git: IDLE_GIT,
  catalog: null,
  catalogError: null,
  catalogReady: false,
  reloadCatalog: () => undefined,
  patch: async () => undefined,
  refreshCatalog: async () => undefined,
  refreshing: false,
  wake: () => undefined,
  touch: () => undefined,
});

export function ClaudeConfigProvider({ available, children }: { available: boolean; children: ReactNode }) {
  const [badgeEpoch, setBadgeEpoch] = useState(0);
  const [sectionEpoch, setSectionEpoch] = useState(0);
  const onChanged = useCallback(() => setBadgeEpoch((n) => n + 1), []);
  const onCommitted = useCallback(() => {
    setBadgeEpoch((n) => n + 1);
    setSectionEpoch((n) => n + 1);
  }, []);
  const [awake, setAwake] = useState(false);
  const wake = useCallback(() => setAwake(true), []);
  const live = available && awake;
  const git = useGitStatus(badgeEpoch, live);

  const [catalog, setCatalog] = useState<PreferencesGet | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogEpoch, setCatalogEpoch] = useState(0);
  const reloadCatalog = useCallback(() => setCatalogEpoch((n) => n + 1), []);
  useEffect(() => {
    if (!live) return;
    let alive = true;
    cc.preferences.get().then(
      (d) => {
        if (!alive) return;
        setCatalog(d);
        setCatalogError(null);
      },
      (e) => {
        if (!alive) return;
        // A reload that failed after a write must not leave the old values
        // on screen with live reset buttons (CodeRabbit on #1777).
        setCatalog(null);
        setCatalogError((e as Error).message);
      },
    );
    return () => {
      alive = false;
    };
  }, [live, catalogEpoch, sectionEpoch]);

  const touch = useCallback((section: string) => touchSection(section), []);
  const patch = useCallback(
    async (key: string, value: unknown) => {
      try {
        const res = await cc.preferences.patch({ [key]: value });
        if (!res.ok) toastErr(res.error || "Save failed");
        else {
          toastOk("Saved");
          onChanged();
          const section = catalog?.schema.find((d) => d.key === key)?.group;
          if (section) touchSection(section);
        }
      } catch (e) {
        toastErr((e as Error).message);
      }
      reloadCatalog();
    },
    [onChanged, reloadCatalog, catalog],
  );

  const [refreshing, setRefreshing] = useState(false);
  const refreshCatalog = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await cc.refreshCatalog();
      if (!res.ok) {
        toastErr(res.error || "Refresh failed");
        return;
      }
      const extra = res.undocumented?.length ? ` · ${res.undocumented.length} undocumented` : "";
      toastOk(`Catalog refreshed: ${res.updated}/${res.total} entries${extra}`);
      reloadCatalog();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  }, [reloadCatalog]);

  const value = useMemo<ClaudeStore>(
    () => ({
      available,
      badgeEpoch,
      sectionEpoch,
      onChanged,
      onCommitted,
      git,
      catalog,
      catalogError,
      // On `available`, not `live`: the page wakes the store in an effect, and
      // the render before that effect must still count as waiting.
      catalogReady: !available || catalog !== null || catalogError !== null,
      reloadCatalog,
      patch,
      refreshCatalog,
      refreshing,
      wake,
      touch,
    }),
    [
      available,
      live,
      badgeEpoch,
      sectionEpoch,
      onChanged,
      onCommitted,
      git,
      catalog,
      catalogError,
      reloadCatalog,
      patch,
      refreshCatalog,
      refreshing,
      wake,
      touch,
    ],
  );
  return <ClaudeContext.Provider value={value}>{children}</ClaudeContext.Provider>;
}

// -- the settings.json rows ----------------------------------------------------

export const CLAUDE_PREF_PREFIX = "claude-pref:";

export interface ClaudePrefEntry extends SettingEntry {
  id: string;
  prefKey: string;
}

export function isClaudePref(e: SettingEntry): e is ClaudePrefEntry {
  return e.id.startsWith(CLAUDE_PREF_PREFIX);
}

/** One row per catalog entry, in catalog order, sectioned by its group. */
export function claudePrefEntries(schema: readonly PrefEntry[]): ClaudePrefEntry[] {
  return schema.map((d) => ({
    id: CLAUDE_PREF_PREFIX + d.key,
    prefKey: d.key,
    group: "claude",
    section: d.group,
    label: d.label,
    description: d.doc || "",
    keywords: `${d.key} ${d.group} ${(d.options || []).join(" ")} claude settings.json`,
  }));
}

/** The catalog's rows on this machine, or none until it has loaded. `wanted`
 *  wakes the store: the rows exist once the group is shown or a search runs. */
export function useClaudePrefEntries(wanted: boolean): { rows: ClaudePrefEntry[]; ready: boolean } {
  const { catalog, catalogReady, wake } = useContext(ClaudeContext);
  useEffect(() => {
    if (wanted) wake();
  }, [wanted, wake]);
  const rows = useMemo(() => (catalog ? claudePrefEntries(catalog.schema) : []), [catalog]);
  return { rows, ready: !wanted || catalogReady };
}

function unsetLabel(d: PrefEntry): string {
  if (d.unsetLabel) return d.unsetLabel;
  if (d.default !== null && d.default !== undefined) return `default: ${JSON.stringify(d.default)}`;
  return "Claude default";
}

function boolDefault(d: PrefEntry): boolean | null {
  return typeof d.default === "boolean" ? d.default : null;
}

function hasDefault(d: PrefEntry): boolean {
  return !!d.unsetLabel || (d.default !== null && d.default !== undefined);
}

/** The status line of an unset row: Claude's documented default, in words.
 *  A select or a field with no documented default already says "Claude
 *  default" in its own placeholder, so it gets no line. */
function defaultLine(d: PrefEntry): string {
  if (d.unsetLabel) return d.unsetLabel;
  if (d.control === "toggle") {
    const b = boolDefault(d);
    return b === null ? "Claude's default" : `Claude's default: ${b ? "on" : "off"}`;
  }
  if (d.default !== null && d.default !== undefined) {
    const v = d.control === "select" ? optionLabel(d, String(d.default)) : JSON.stringify(d.default);
    return `Claude's default: ${v}`;
  }
  return "Claude's default";
}

function ScalarControl({
  entry,
  value,
  onCommit,
}: {
  entry: PrefEntry;
  value: unknown;
  onCommit: (next: string | number | null) => void;
}) {
  const stored = value === null || value === undefined ? "" : String(value);
  const [draft, setDraft] = useState(stored);
  useEffect(() => setDraft(stored), [stored]);

  const commit = () => {
    const v = draft.trim();
    if (v === stored) return; // nothing changed — don't spend a commit on it
    if (v === "") {
      onCommit(null);
      return;
    }
    if (entry.control === "number") {
      const n = Number(v);
      if (!Number.isFinite(n)) {
        toastErr(`${entry.label}: "${v}" is not a number`);
        setDraft(stored);
        return;
      }
      onCommit(n);
      return;
    }
    onCommit(v);
  };

  return (
    <Input
      type={entry.control === "number" ? "number" : "text"}
      aria-label={entry.label}
      value={draft}
      placeholder={
        entry.control === "number"
          ? entry.default === null || entry.default === undefined
            ? ""
            : String(entry.default)
          : unsetLabel(entry)
      }
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
      }}
    />
  );
}

/** One settings.json key: the row, with a reset once it differs from its
 *  default or a quiet hint of the default while it does not. */
export function ClaudePrefRow({ entry }: { entry: ClaudePrefEntry }) {
  const store = useContext(ClaudeContext);
  const d = store.catalog?.schema.find((x) => x.key === entry.prefKey);
  if (!d || !store.catalog) return null;
  const val = store.catalog.prefs[d.key];
  const isSet = val !== null && val !== undefined;
  const patch = (next: unknown) => void store.patch(d.key, next);
  return (
    <SettingRow
      id={entry.id}
      entry={entry}
      // Unset: what Claude does instead, said under the description — a line
      // of words, not an icon (the ⓘ "looked disabled and said nothing",
      // Akshil 2026-10-20). Set: the reset beside the control says the rest.
      status={isSet || (d.control !== "toggle" && !hasDefault(d)) ? undefined : defaultLine(d)}
      control={
        <span className="settings-claude-control">
          {isSet && (
            <Button
              variant="ghost"
              size="icon-sm"
              title="Reset to default"
              aria-label={`Reset ${d.label} to default`}
              onClick={() => patch(null)}
            >
              <Undo2 />
            </Button>
          )}
          {d.control === "toggle" && (
            // Unset draws Claude's documented default, dimmed: the position is
            // Claude's, not the reader's, until they press it.
            <Switch
              aria-labelledby={labelId(entry.id)}
              checked={isSet ? !!val : boolDefault(d) === true}
              onCheckedChange={(c) => patch(!!c)}
              className={cn("settings-switch", !isSet && "settings-switch-inherited")}
            />
          )}
          {d.control === "select" && (
            <SettingSelect
              id={entry.id}
              value={isSet ? String(val) : ""}
              options={[
                { key: "", label: `— ${unsetLabel(d)} —` },
                // The value on disk when the catalog doesn't list it: a key that
                // IS set (model: "fable") must not render as "Claude default".
                ...(isSet && !(d.options || []).includes(String(val))
                  ? [{ key: String(val), label: storedOptionLabel(d, String(val)) }]
                  : []),
                ...(d.options || []).map((o) => ({ key: o, label: optionLabel(d, o) })),
              ]}
              onChange={(v) => patch(v === "" ? null : v)}
            />
          )}
          {(d.control === "text" || d.control === "number") && (
            <ScalarControl entry={d} value={val} onCommit={patch} />
          )}
        </span>
      }
    />
  );
}

/** The row that leads the settings.json rows: how many, and the refresh. */
export function ClaudeCatalogControl() {
  const store = useContext(ClaudeContext);
  const n = store.catalog?.schema.length;
  return (
    <SettingRow
      id="claude-catalog"
      status={n !== undefined ? `${n} settings in the catalog` : store.catalogError ? null : "Loading…"}
      control={
        <Button
          variant="outline"
          size="sm"
          disabled={store.refreshing || !store.available}
          title="Re-fetch defaults & docs from code.claude.com and rewrite settings_catalog.json"
          onClick={() => void store.refreshCatalog()}
        >
          <RefreshCw data-icon="inline-start" className={cn(store.refreshing && "animate-spin")} />
          {store.refreshing ? "Refreshing…" : "Refresh catalog"}
        </Button>
      }
    >
      {store.catalogError && <ErrorBanner>{store.catalogError}</ErrorBanner>}
    </SettingRow>
  );
}

// -- the blocks ----------------------------------------------------------------

/** A panel section as a block of its group. In search results the block is a
 *  door to itself instead — a plugin list is not a search hit. */
function Block({ id, children }: { id: SettingId; children: ReactNode }) {
  const view = useContext(SettingsViewContext);
  const { sectionEpoch } = useContext(ClaudeContext);
  if (view.tokens) return <PointerRow id={id} to={() => view.jump(id)} label="Claude Code" />;
  return (
    <div key={sectionEpoch} className="settings-block" id={rowId(id)} data-setting={id}>
      {children}
    </div>
  );
}

/** A block's `onChanged`, also noting the edit for the section order. */
function useBlockChanged(section: string): () => void {
  const { onChanged, touch } = useContext(ClaudeContext);
  return useCallback(() => {
    touch(section);
    onChanged();
  }, [section, onChanged, touch]);
}

export function ClaudePluginsBlock() {
  const changed = useBlockChanged("Plugins");
  return (
    <Block id="claude-plugins">
      <PluginsSection onChanged={changed} />
    </Block>
  );
}

export function ClaudeMemoryBlock() {
  const changed = useBlockChanged("Memory");
  return (
    <Block id="claude-memory">
      <MemorySection onChanged={changed} />
    </Block>
  );
}

export function ClaudeSkillsBlock() {
  return (
    <Block id="claude-skills">
      <SkillsSection />
    </Block>
  );
}

export function ClaudeStatuslineBlock() {
  return (
    <Block id="claude-statusline">
      <StatuslineSection />
    </Block>
  );
}

export function ClaudeMcpBlock() {
  return (
    <Block id="claude-mcp">
      <McpSection />
    </Block>
  );
}

export function ClaudeHistoryBlock() {
  const { onCommitted, badgeEpoch } = useContext(ClaudeContext);
  const onChanged = useBlockChanged("History");
  // Keyed on EVERY edit, not only on a commit: the card reads its own git
  // status and log, and a plugin toggle or a settings.json write above it is
  // drift it must show beside the chip that already counts it. (The panel had
  // the same freshness by remounting History on every tab switch.)
  return (
    <Block id="claude-history">
      <HistorySection key={badgeEpoch} onChanged={onChanged} onCommitted={onCommitted} />
    </Block>
  );
}

// -- the git chip ---------------------------------------------------------------

/** Beside the group's title: Clean / N uncommitted / Status unknown, and the
 *  way down to the History block where the drift is committed. Three states,
 *  not two: it states "Clean" POSITIVELY, so it must never say that from a
 *  null status — during the first read, or forever if the read keeps failing. */
export function ClaudeGitChip({ onOpen }: { onOpen?: () => void }) {
  const { git } = useContext(ClaudeContext);
  const { status, failed } = git;
  return (
    <button
      type="button"
      className={cn("cc-gitchip", status?.dirty && "dirty", (!status || failed) && "unknown")}
      title={
        failed
          ? "Git status unavailable — could not be reached"
          : !status
            ? "Checking git status…"
            : status.dirty
              ? `${status.files.length} uncommitted change(s) — review and commit them under History`
              : "Commits, profiles and uncommitted changes"
      }
      onClick={() => {
        onOpen?.();
        // After the open has painted: a closed section has no block to scroll to.
        requestAnimationFrame(() =>
          document.getElementById(rowId("claude-history"))?.scrollIntoView({ block: "start", behavior: "smooth" }),
        );
      }}
    >
      <span className="cc-gitchip-dot" aria-hidden="true" />
      {failed ? "Status unknown" : !status ? "Checking…" : status.dirty ? `${status.files.length} uncommitted` : "Clean"}
      <Clock aria-hidden="true" className="size-3" />
    </button>
  );
}

// -- the section layout -----------------------------------------------------------
// ACCORDIONS, CLOSED BY DEFAULT (Akshil, 2026-10-20): eighteen sections on one
// page is a wall; a reader opens the one they came for. Model & reasoning is
// first and open — it is the one nearly everyone came for. The rest are ordered
// by when the reader last changed something in them, so a section they keep
// coming back to rises; the order is read ONCE per visit, never re-sorted
// under the pointer. Both facts live in localStorage: they are about this
// reader's habits on this machine, not the config.

const LAYOUT_KEY = "fused.prefs.claude-sections";
export const FIRST_SECTION = "Model & reasoning";

interface SectionLayout {
  open: string[];
  edited: Record<string, number>;
}

function readLayout(): SectionLayout {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (raw) {
      const v = JSON.parse(raw) as Partial<SectionLayout>;
      return { open: Array.isArray(v.open) ? v.open : [FIRST_SECTION], edited: v.edited ?? {} };
    }
  } catch {
    /* fall through */
  }
  return { open: [FIRST_SECTION], edited: {} };
}

function writeLayout(l: SectionLayout): void {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(l));
  } catch {
    /* private mode, full — the order is a convenience */
  }
}

function touchSection(section: string): void {
  const l = readLayout();
  l.edited[section] = Date.now();
  writeLayout(l);
}

/** The sections in the order a visit opens them in: the first one, then the
 *  recently edited newest first, then the rest as the page lists them. */
export function orderClaudeSections(names: readonly string[], edited: Record<string, number>): string[] {
  const rest = names.filter((n) => n !== FIRST_SECTION);
  const recent = rest.filter((n) => edited[n]).sort((a, b) => edited[b] - edited[a]);
  const others = rest.filter((n) => !edited[n]);
  return [...(names.includes(FIRST_SECTION) ? [FIRST_SECTION] : []), ...recent, ...others];
}

export interface ClaudeSectionLayout {
  order: (names: readonly string[]) => string[];
  isOpen: (name: string) => boolean;
  toggle: (name: string) => void;
  ensureOpen: (name: string) => void;
}

export function useClaudeSectionLayout(): ClaudeSectionLayout {
  const [layout, setLayout] = useState<SectionLayout>(readLayout);
  // The edit stamps as they were when the page opened: an edit made now
  // changes the NEXT visit's order, not this one's.
  const [editedAtOpen] = useState(() => layout.edited);
  const update = useCallback((f: (l: SectionLayout) => SectionLayout) => {
    setLayout((prev) => {
      const next = f({ ...prev, edited: readLayout().edited });
      writeLayout(next);
      return next;
    });
  }, []);
  return useMemo(
    () => ({
      order: (names) => orderClaudeSections(names, editedAtOpen),
      isOpen: (name) => layout.open.includes(name),
      toggle: (name) =>
        update((l) => ({ ...l, open: l.open.includes(name) ? l.open.filter((n) => n !== name) : [...l.open, name] })),
      ensureOpen: (name) => update((l) => (l.open.includes(name) ? l : { ...l, open: [...l.open, name] })),
    }),
    [layout.open, editedAtOpen, update],
  );
}
