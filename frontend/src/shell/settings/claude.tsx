// The "Claude Code" group of Preferences: the settings.json keys that
// shape the Claude sessions fused-render runs. The catalog (GET preferences)
// is fetched with the page and each entry becomes a row, `claude-pref:<key>`,
// bucketed into the catalog's own groups ("Model & reasoning", ...), so a
// search for "effort" lands on the row itself. The registry cannot list them:
// they are data the server ships. `ClaudeConfigProvider` wraps the page and
// holds the catalog, the stored values and the write.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Undo2 } from "lucide-react";
import * as cc from "@apps/claude_config/api";
import type { PrefEntry, PreferencesGet } from "@apps/claude_config/api";
import { toastErr, toastOk } from "@apps/claude_config/bits";
import { optionLabel, storedOptionLabel } from "@apps/claude_config/pref-option-label";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { cn } from "@platform/lib/utils";
import type { SettingEntry } from "./registry";
import { labelId, SettingRow, SettingSelect } from "./SettingRow";

// -- the store ---------------------------------------------------------------

interface ClaudeStore {
  catalog: PreferencesGet | null;
  /** The catalog GET has answered (rows or an error): the page can count. */
  catalogReady: boolean;
  patch: (key: string, value: unknown) => Promise<void>;
}

const ClaudeContext = createContext<ClaudeStore>({
  catalog: null,
  catalogReady: false,
  patch: async () => undefined,
});

export function ClaudeConfigProvider({ available, children }: { available: boolean; children: ReactNode }) {
  const [catalog, setCatalog] = useState<PreferencesGet | null>(null);
  const [failed, setFailed] = useState(false);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    if (!available) return;
    let alive = true;
    cc.preferences.get().then(
      (d) => {
        if (!alive) return;
        setCatalog(d);
        setFailed(false);
      },
      (e) => {
        if (!alive) return;
        // A reload that failed after a write must not leave the old values
        // on screen with live reset buttons.
        setCatalog(null);
        setFailed(true);
        toastErr((e as Error).message);
      },
    );
    return () => {
      alive = false;
    };
  }, [available, epoch]);

  const patch = useCallback(async (key: string, value: unknown) => {
    try {
      const res = await cc.preferences.patch({ [key]: value });
      if (!res.ok) toastErr(res.error || "Save failed");
      else toastOk("Saved");
    } catch (e) {
      toastErr((e as Error).message);
    }
    setEpoch((n) => n + 1);
  }, []);

  const value = useMemo<ClaudeStore>(
    () => ({ catalog, catalogReady: !available || catalog !== null || failed, patch }),
    [available, catalog, failed, patch],
  );
  return <ClaudeContext.Provider value={value}>{children}</ClaudeContext.Provider>;
}

// -- the settings.json rows ----------------------------------------------------

export const CLAUDE_PREF_PREFIX = "claude-pref:";

/** A settings.json key as a settings row of the Claude Code group. */
export interface ClaudePrefEntry extends SettingEntry {
  id: string;
  prefKey: string;
}

/** One row per catalog entry, in catalog order, sectioned by its group. */
export function claudePrefEntries(schema: readonly PrefEntry[]): ClaudePrefEntry[] {
  return schema.map((d) => ({
    id: CLAUDE_PREF_PREFIX + d.key,
    prefKey: d.key,
    group: "claude-cli",
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


export function isClaudePref(e: { id: string }): e is ClaudePrefEntry {
  return e.id.startsWith(CLAUDE_PREF_PREFIX);
}

/** The catalog's rows on this machine, or none until it has loaded. */
export function useClaudePrefEntries(): { rows: ClaudePrefEntry[]; ready: boolean } {
  const { catalog, catalogReady } = useContext(ClaudeContext);
  const rows = useMemo(() => (catalog ? claudePrefEntries(catalog.schema) : []), [catalog]);
  return { rows, ready: catalogReady };
}

/** The group's one row on a machine without ~/.claude. */
export function ClaudeCliMissing() {
  return <SettingRow id="claude-cli-missing" />;
}
