// Preferences section: the curated settings.json catalog as form controls.
//
// Schema-driven — the catalog `preferences.get` returns decides the groups, the
// order, the control per key and the documented default. Nothing about a
// specific setting is hardcoded here, which is the point: `refresh_catalog`
// rewrites the docs half of that catalog from Anthropic's reference and the UI
// picks the change up without a code change.
//
// Unset is a first-class state, never "off"/"empty": a key with no value in
// settings.json is using Claude's OWN default, which may well be `true`. So a
// toggle renders indeterminate, a select shows an italic "— default: … —"
// placeholder, and the reset link (which patches null, deleting the leaf) only
// appears once a key actually has a value.
import { useCallback, useEffect, useState } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { PrefEntry } from "../api";
import {
  Group,
  Icon,
  Row,
  SKELETON_ROWS,
  SectionToolbar,
  Toggle3,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

// What to show where a value would be, when there is none. The catalog's own
// `unsetLabel` wins; otherwise the documented default, else the honest "we
// don't know what Claude does by default".
function unsetLabel(d: PrefEntry): string {
  if (d.unsetLabel) return d.unsetLabel;
  if (d.default !== null && d.default !== undefined) return `default: ${JSON.stringify(d.default)}`;
  return "Claude default";
}

// A text/number field. Local draft state so typing isn't a write per keystroke;
// the value is committed on blur or Enter, exactly where the original app's
// `onchange` fired. Re-syncs when the server value changes under it (a reload
// after some other key's patch).
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
    <input
      className="field-control cc-scalar"
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

export default function PreferencesSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.preferences.get(), []);
  const { data, error, reload } = useModuleData(load);
  const [refreshing, setRefreshing] = useState(false);

  // One key at a time, as the original did: each patch is its own commit in the
  // config repo, so the history reads as a list of decisions rather than one
  // opaque "Update preferences".
  const patch = async (key: string, value: unknown) => {
    try {
      const res = await cc.preferences.patch({ [key]: value });
      if (!res.ok) {
        toastErr(res.error || "Save failed");
      } else {
        toastOk("Saved");
        onChanged();
      }
    } catch (e) {
      toastErr((e as Error).message);
    }
    // Reload either way: on success to pick up the true on-disk value, on
    // failure to snap the control back off the value that never landed.
    reload();
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      const res = await cc.refreshCatalog();
      if (!res.ok) {
        toastErr(res.error || "Refresh failed");
        return;
      }
      const extra = res.undocumented?.length ? ` · ${res.undocumented.length} undocumented` : "";
      toastOk(`Catalog refreshed: ${res.updated}/${res.total} entries${extra}`);
      reload();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={SKELETON_ROWS} label="Loading preferences" />;

  const groups: { name: string; items: PrefEntry[] }[] = [];
  for (const d of data.schema) {
    let g = groups.find((x) => x.name === d.group);
    if (!g) {
      g = { name: d.group, items: [] };
      groups.push(g);
    }
    g.items.push(d);
  }

  return (
    <>
      {/* Two different acts, deliberately not merged: the icon re-reads
          settings.json (what every other tab's refresh does), while "Refresh
          catalog" re-fetches Anthropic's docs and rewrites the catalog itself.
          Collapsing the second into an icon would hide a network write behind
          the same glyph as a local re-read. */}
      <SectionToolbar summary={`${data.schema.length} settings from the checked-in catalog`}
        onRefresh={reload}>
        <button
          type="button"
          className="btn"
          disabled={refreshing}
          title="Re-fetch defaults & docs from code.claude.com and rewrite settings_catalog.json"
          onClick={refresh}
        >
          <Icon name="refresh" />
          {refreshing ? "Refreshing…" : "Refresh catalog"}
        </button>
      </SectionToolbar>
      {groups.map((g) => (
        <Group key={g.name} title={g.name}>
          {g.items.map((d) => {
            const val = data.prefs[d.key];
            const isSet = val !== null && val !== undefined;
            return (
              <Row
                key={d.key}
                label={d.label}
                doc={d.doc || null}
                control={
                  <>
                    {isSet ? (
                      <button
                        type="button"
                        className="cc-reset"
                        title="Reset to default"
                        onClick={() => patch(d.key, null)}
                      >
                        reset
                      </button>
                    ) : (
                      <span className="cc-unset">{unsetLabel(d)}</span>
                    )}
                    {d.control === "toggle" && (
                      <Toggle3
                        label={d.label}
                        value={isSet ? !!val : null}
                        onChange={(next) => patch(d.key, next)}
                      />
                    )}
                    {d.control === "select" && (
                      <select
                        className="field-control"
                        aria-label={d.label}
                        value={isSet ? String(val) : ""}
                        onChange={(e) => patch(d.key, e.target.value === "" ? null : e.target.value)}
                      >
                        <option value="">— {unsetLabel(d)} —</option>
                        {/* The value on disk when the catalog doesn't list it.
                            settings.json is not validated against our curated
                            options, and a select with no matching option falls
                            back to the placeholder — i.e. a key that IS set
                            (model: "fable") would render as "Claude default".
                            Showing it as its own option is the difference
                            between reporting the file and contradicting it. */}
                        {isSet && !(d.options || []).includes(String(val)) && (
                          <option value={String(val)}>{String(val)} (not in catalog)</option>
                        )}
                        {(d.options || []).map((o) => (
                          <option key={o} value={o}>
                            {o}
                          </option>
                        ))}
                      </select>
                    )}
                    {(d.control === "text" || d.control === "number") && (
                      <ScalarControl
                        entry={d}
                        value={val}
                        onCommit={(next) => patch(d.key, next)}
                      />
                    )}
                  </>
                }
              />
            );
          })}
        </Group>
      ))}
    </>
  );
}
