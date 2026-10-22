// The tabs of the Claude Code page: Claude Code's own configuration under
// ~/.claude, drawn with the settings page's rows and cards.
//
// TWO THINGS LIVE HERE:
//
//   * the settings.json rows. The catalog (GET preferences) is fetched once
//     per mount and each of its entries becomes a row, bucketed into the
//     catalog's own groups ("Model & reasoning", …). The registry cannot list
//     them: they are data the server ships.
//
//   * the provider that fetches that catalog once per mount, and wakes only
//     when a tab that needs it mounts.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Undo2 } from "lucide-react";
import * as cc from "@apps/claude_config/api";
import type { PrefEntry, PreferencesGet } from "@apps/claude_config/api";
import { Head, toastErr, toastOk } from "@apps/claude_config/bits";
import { optionLabel, storedOptionLabel } from "@apps/claude_config/pref-option-label";
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
  catalog: PreferencesGet | null;
  catalogError: string | null;
  /** The catalog GET has answered (rows or an error): the page can count. */
  catalogReady: boolean;
  reloadCatalog: () => void;
  patch: (key: string, value: unknown) => Promise<void>;
  /** Start the reads. The provider wraps the whole page; a tab that needs the
   *  catalog calls this when it mounts. */
  wake: () => void;
}

const ClaudeContext = createContext<ClaudeStore>({
  available: false,
  catalog: null,
  catalogError: null,
  catalogReady: false,
  reloadCatalog: () => undefined,
  patch: async () => undefined,
  wake: () => undefined,
});

export function ClaudeConfigProvider({ available, children }: { available: boolean; children: ReactNode }) {
  const [awake, setAwake] = useState(false);
  const wake = useCallback(() => setAwake(true), []);
  const live = available && awake;

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
  }, [live, catalogEpoch]);

  const patch = useCallback(
    async (key: string, value: unknown) => {
      try {
        const res = await cc.preferences.patch({ [key]: value });
        if (!res.ok) toastErr(res.error || "Save failed");
        else toastOk("Saved");
      } catch (e) {
        toastErr((e as Error).message);
      }
      reloadCatalog();
    },
    [reloadCatalog],
  );

  const value = useMemo<ClaudeStore>(
    () => ({
      available,
      catalog,
      catalogError,
      // On `available`, not `live`: the page wakes the store in an effect, and
      // the render before that effect must still count as waiting.
      catalogReady: !available || catalog !== null || catalogError !== null,
      reloadCatalog,
      patch,
      wake,
    }),
    [available, catalog, catalogError, reloadCatalog, patch, wake],
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

type HeadProps = { title: string; blurb: string };

// -- the tabs ------------------------------------------------------------------

/** Settings tab: every settings.json key in cards by the catalog's group. */
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

export function ClaudePluginsTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <PluginsSection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}

export function ClaudeMemoryTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <MemorySection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}

export function ClaudeSkillsTab({ filter }: { filter: string }) {
  return <SkillsSection filter={filter} />;
}

export function ClaudeMcpTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <McpSection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}
