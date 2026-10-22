// The tabs of the Claude Code page: Claude Code's own configuration under
// ~/.claude, drawn with the settings page's rows and cards.
//
// TWO THINGS LIVE HERE AND NOWHERE ELSE:
//
//   * the settings.json rows. The catalog (GET preferences) is fetched once
//     per mount and each of its entries becomes a row, bucketed into the
//     catalog's own groups ("Model & reasoning", …). The registry cannot list
//     them: they are data the server ships, refreshed from Anthropic's docs by
//     the Refresh catalog button.
//
//   * the git epoch. Every section can dirty the repo (`onChanged`), the
//     History section commits it (`onCommitted`), and the chip on the History
//     tab's header carries the dirty state. A commit rewrites state no section
//     could have predicted, so each remounts on the commit epoch and refetches.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Clock, RefreshCw, Undo2 } from "lucide-react";
import * as cc from "@apps/claude_config/api";
import type { PrefEntry, PreferencesGet } from "@apps/claude_config/api";
import { Head, toastErr, toastOk, useGitStatus, type GitStatusState } from "@apps/claude_config/bits";
import { optionLabel, storedOptionLabel } from "@apps/claude_config/pref-option-label";
import HistorySection from "@apps/claude_config/sections/HistorySection";
import McpSection from "@apps/claude_config/sections/McpSection";
import MemorySection from "@apps/claude_config/sections/MemorySection";
import PluginsSection from "@apps/claude_config/sections/PluginsSection";
import SkillsSection from "@apps/claude_config/sections/SkillsSection";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { cn } from "@platform/lib/utils";
import type { SettingEntry } from "../settings/registry";
import { labelId, SettingRow, SettingSelect } from "../settings/SettingRow";

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
  /** Start the reads. The provider wraps the whole page; a tab that needs the
   *  git status or the catalog calls this when it mounts. */
  wake: () => void;
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

  const patch = useCallback(
    async (key: string, value: unknown) => {
      try {
        const res = await cc.preferences.patch({ [key]: value });
        if (!res.ok) toastErr(res.error || "Save failed");
        else {
          toastOk("Saved");
          onChanged();
        }
      } catch (e) {
        toastErr((e as Error).message);
      }
      reloadCatalog();
    },
    [onChanged, reloadCatalog],
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
    ],
  );
  return <ClaudeContext.Provider value={value}>{children}</ClaudeContext.Provider>;
}

// -- the settings.json rows ----------------------------------------------------

export const CLAUDE_PREF_PREFIX = "claude-pref:";

/** A settings.json key as a settings row. The row only reads `label`,
 *  `description` and `id`; `group` is required by the type and unread outside
 *  a search, which the Claude Code page does not run. */
export interface ClaudePrefEntry extends SettingEntry {
  id: string;
  prefKey: string;
}

/** One row per catalog entry, in catalog order, sectioned by its group. */
export function claudePrefEntries(schema: readonly PrefEntry[]): ClaudePrefEntry[] {
  return schema.map((d) => ({
    id: CLAUDE_PREF_PREFIX + d.key,
    prefKey: d.key,
    group: "ai",
    section: d.group,
    label: d.label,
    description: d.doc || "",
    keywords: "",
  }));
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

/** The catalog row is built here, not in the registry: the registry only
 *  lists Preferences rows, and this one lives on the Claude Code page. */
const CATALOG_ENTRY: SettingEntry = {
  id: "claude-catalog",
  group: "ai",
  section: "settings.json",
  label: "Settings catalog",
  description: "The settings below come from the checked-in catalog. Refresh re-fetches defaults and docs from code.claude.com and rewrites it.",
  keywords: "",
};

/** The row that leads the settings.json rows: how many, and the refresh. */
export function ClaudeCatalogControl() {
  const store = useContext(ClaudeContext);
  const n = store.catalog?.schema.length;
  return (
    <SettingRow
      id="claude-catalog"
      entry={CATALOG_ENTRY}
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

type HeadProps = { title: string; blurb: string };

// -- the tabs ------------------------------------------------------------------

/** Settings tab: every settings.json key in cards by the catalog's group,
 *  led by the catalog's refresh. */
export function ClaudeSettingsTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  const { catalog, catalogError, wake, available } = useContext(ClaudeContext);
  useEffect(() => wake(), [wake]);
  const rows = useMemo(() => (catalog ? claudePrefEntries(catalog.schema) : []), [catalog]);
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? rows.filter((r) => `${r.prefKey} ${r.label} ${r.description}`.toLowerCase().includes(needle))
    : rows;
  const sections = [...new Set(shown.map((r) => r.section))];
  if (!available)
    return (
      <>
        <Head title={title} blurb={blurb} />
        <ErrorBanner>~/.claude was not found on this machine.</ErrorBanner>
      </>
    );
  return (
    <>
      <Head title={title} blurb={blurb} />
      <section className="settings-section" aria-label="Catalog">
        <div className="settings-card">
          <ClaudeCatalogControl />
        </div>
      </section>
      {catalogError && <ErrorBanner>{catalogError}</ErrorBanner>}
      {sections.map((sec) => (
        <section key={sec} className="settings-section" aria-label={sec}>
          <h3 className="settings-section-title">{sec}</h3>
          <div className="settings-card">
            {shown
              .filter((r) => r.section === sec)
              .map((r) => (
                <ClaudePrefRow key={r.id} entry={r} />
              ))}
          </div>
        </section>
      ))}
    </>
  );
}

function Block({ children }: { children: ReactNode }) {
  const { sectionEpoch } = useContext(ClaudeContext);
  return (
    <div key={sectionEpoch} className="settings-block">
      {children}
    </div>
  );
}

export function ClaudePluginsTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  const { onChanged } = useContext(ClaudeContext);
  return (
    <Block>
      <PluginsSection onChanged={onChanged} filter={filter} title={title} blurb={blurb} />
    </Block>
  );
}

export function ClaudeMemoryTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  const { onChanged } = useContext(ClaudeContext);
  return (
    <Block>
      <MemorySection onChanged={onChanged} filter={filter} title={title} blurb={blurb} />
    </Block>
  );
}

export function ClaudeSkillsTab({ filter }: { filter: string }) {
  const { onChanged, sectionEpoch } = useContext(ClaudeContext);
  return (
    <SkillsSection key={sectionEpoch} onChanged={onChanged} filter={filter} />
  );
}

export function ClaudeMcpTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  const { onChanged } = useContext(ClaudeContext);
  return (
    <Block>
      <McpSection onChanged={onChanged} filter={filter} title={title} blurb={blurb} />
    </Block>
  );
}

export function ClaudeHistoryTab() {
  const { onCommitted, onChanged, badgeEpoch } = useContext(ClaudeContext);
  // Keyed on EVERY edit, not only on a commit: the card reads its own git
  // status and log, and a plugin toggle or a settings.json write elsewhere is
  // drift it must show beside the chip that already counts it.
  return (
    <Block>
      <HistorySection key={badgeEpoch} onChanged={onChanged} onCommitted={onCommitted} />
    </Block>
  );
}

// -- the git chip ---------------------------------------------------------------

/** On the History tab's header: Clean / N uncommitted / Status unknown.
 *  (`onOpen` — the page navigates to History.) Three states, not two: it states "Clean"
 *  POSITIVELY, so it must never say that from a null status — during the
 *  first read, or forever if the read keeps failing. */
export function ClaudeGitChip({ onOpen }: { onOpen: () => void }) {
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
      onClick={onOpen}
    >
      <span className="cc-gitchip-dot" aria-hidden="true" />
      {failed ? "Status unknown" : !status ? "Checking…" : status.dirty ? `${status.files.length} uncommitted` : "Clean"}
      <Clock aria-hidden="true" className="size-3" />
    </button>
  );
}
