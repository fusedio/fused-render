// Profiles section: a profile is a git branch over the Claude config dir, so
// creating one forks, switching one checks out, and exporting one archives the
// branch's tracked files.
//
// Two invariants shape the whole section:
//
//   * Nothing is switched or imported over a dirty tree without saying so. Both
//     actions refuse with {dirty, files} when there is uncommitted drift, and the
//     UI turns that refusal into a "Commit & …" confirm which retries with a
//     commit message — the retry is the ONLY thing that makes the module commit.
//   * A successful switch/import reloads the page. Every other section has
//     already read the config that just got swapped underneath it, and a
//     targeted refresh of ten sections is a worse contract than one reload.
import { useCallback, useState } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { DirtyRefusal, ZipEntry } from "../api";
import {
  Card,
  CardActions,
  CardSub,
  CardTitle,
  Pill,
  fileToB64,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

// The import picker's file tree, bucketed by top-level folder in first-seen
// order. Root-level files (no "/") share one group keyed by "".
interface PickGroup {
  key: string;
  label: string;
  paths: string[];
}

function bucket(entries: ZipEntry[]): PickGroup[] {
  const groups: PickGroup[] = [];
  for (const e of entries) {
    if (e.isDir) continue;
    const key = e.path.includes("/") ? e.path.slice(0, e.path.indexOf("/")) : "";
    let g = groups.find((x) => x.key === key);
    if (!g) {
      g = { key, label: key === "" ? "Root files" : key + "/", paths: [] };
      groups.push(g);
    }
    g.paths.push(e.path);
  }
  return groups;
}

// A checkbox whose `indeterminate` state is driven by its children — the group
// and select-all rows of the import picker. (Same DOM-property-only problem as
// the tri-state toggle in bits.tsx.)
function TriCheckbox({
  checked,
  indeterminate,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <input
      // Inline callback ref: React re-runs it on every render (the function's
      // identity changes), which is exactly when the derived indeterminate flag
      // needs re-applying to the DOM property.
      ref={(el) => {
        if (el) el.indeterminate = indeterminate;
      }}
      type="checkbox"
      aria-label={label}
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
    />
  );
}

function ImportPicker({
  entries,
  onCancel,
  onImport,
}: {
  entries: ZipEntry[];
  onCancel: () => void;
  onImport: (branch: string, paths: string[]) => void;
}) {
  const groups = bucket(entries);
  const all = groups.flatMap((g) => g.paths);
  const [branch, setBranch] = useState("");
  const [selected, setSelected] = useState<string[]>(all);

  const isOn = (p: string) => selected.includes(p);
  const setGroup = (g: PickGroup, on: boolean) =>
    setSelected((prev) =>
      on ? [...prev.filter((p) => !g.paths.includes(p)), ...g.paths] : prev.filter((p) => !g.paths.includes(p)),
    );

  const submit = () => {
    const name = branch.trim();
    if (!name) {
      toastErr("new profile name required");
      return;
    }
    if (!selected.length) {
      toastErr("select at least one file");
      return;
    }
    onImport(name, selected);
  };

  return (
    <Modal
      title="Import into a new profile"
      width={620}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={submit}>
            Import
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">New profile name</span>
        <input
          className="field-control"
          placeholder="e.g. imported"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
        />
      </label>
      {all.length === 0 ? (
        <div className="cc-empty">Empty archive.</div>
      ) : (
        <>
          <label className="cc-pick cc-pick-all">
            <TriCheckbox
              label="Select all"
              checked={selected.length === all.length}
              indeterminate={selected.length > 0 && selected.length < all.length}
              onChange={(on) => setSelected(on ? all : [])}
            />
            <span className="cc-unset">Select all</span>
          </label>
          {groups.map((g) => {
            const on = g.paths.filter(isOn).length;
            return (
              <div key={g.key}>
                <label className="cc-pick cc-pick-group">
                  <TriCheckbox
                    label={g.label}
                    checked={on === g.paths.length}
                    indeterminate={on > 0 && on < g.paths.length}
                    onChange={(next) => setGroup(g, next)}
                  />
                  <span className="cc-pick-grouplabel">{g.label}</span>
                </label>
                {g.paths.map((p) => (
                  <label className="cc-pick cc-pick-file" key={p}>
                    <input
                      type="checkbox"
                      checked={isOn(p)}
                      onChange={(e) =>
                        setSelected((prev) =>
                          e.target.checked ? [...prev, p] : prev.filter((x) => x !== p),
                        )
                      }
                    />
                    <span className="cc-mono">{p}</span>
                  </label>
                ))}
              </div>
            );
          })}
        </>
      )}
    </Modal>
  );
}

export default function ProfilesSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.profiles.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  // The staged .zip: held between "inspect" and the picker's decision, so the
  // bytes are read from disk once.
  const [staged, setStaged] = useState<{ b64: string; entries: ZipEntry[] } | null>(null);

  // Turn a {dirty, files} refusal into a "Commit & <verb>" confirm; resolves the
  // commit message to retry with, or null if the user backed out.
  const askCommitFirst = async (res: DirtyRefusal, verb: string, message: string) => {
    const go = await ask<boolean>({
      title: "Uncommitted changes",
      preview: { files: (res.files || []).map((p) => ({ status: "M", path: p })), settings: [] },
      buttons: [
        { label: "Cancel", value: false },
        { label: `Commit & ${verb}`, value: true, primary: true },
      ],
    });
    return go ? message : null;
  };

  const switchInto = async (target: string): Promise<boolean> => {
    const preview = await guard(cc.gitOps.diff(target));
    if (!preview) return false;
    if (preview.error) {
      toastErr(preview.error);
      return false;
    }
    const ok = await ask<boolean>({
      title: `Switch to "${target}"?`,
      preview,
      buttons: [
        { label: "Cancel", value: false },
        { label: "Switch", value: true, primary: true },
      ],
    });
    if (!ok) return false;
    let res = await guard(cc.profiles.switch(target));
    if (!res) return false;
    if (!res.ok && res.dirty) {
      const message = await askCommitFirst(res, "switch", `Save before switching to ${target}`);
      if (!message) return false;
      res = await guard(cc.profiles.switch(target, message));
      if (!res) return false;
    }
    if (!res.ok) {
      toastErr(res.error || "Switch failed");
      return false;
    }
    location.reload();
    return true;
  };

  const create = async () => {
    const name = newName.trim();
    if (!name) {
      toastErr("name required");
      return;
    }
    setBusy(true);
    try {
      const res = await guard(cc.profiles.create(name));
      if (!res) return;
      if (!res.ok) {
        toastErr(res.error || "Create failed");
        return;
      }
      // The branch exists now whatever happens next — if the user cancels the
      // switch it still shows up in the list.
      const switched = await switchInto(name);
      if (!switched) {
        setNewName("");
        onChanged();
        reload();
      }
    } finally {
      setBusy(false);
    }
  };

  const pickZip = async (file: File) => {
    const b64 = await fileToB64(file);
    const info = await guard(cc.profiles.inspect(b64));
    if (!info) return;
    if (!info.ok) {
      toastErr(info.error || "not a valid .zip file");
      return;
    }
    setStaged({ b64, entries: info.entries || [] });
  };

  const runImport = async (branch: string, paths: string[]) => {
    // Read the staged bytes out before clearing, so the retry-with-commit path
    // below doesn't depend on state that has already been dropped.
    const b64 = staged?.b64;
    if (!b64) return;
    setStaged(null);
    let res = await guard(cc.profiles.import(b64, branch, paths));
    if (!res) return;
    if (!res.ok && res.dirty) {
      const message = await askCommitFirst(res, "import", `Save before importing into ${branch}`);
      if (!message) return;
      res = await guard(cc.profiles.import(b64, branch, paths, message));
      if (!res) return;
    }
    if (!res.ok) {
      toastErr(res.error || "Import failed");
      return;
    }
    toastOk(`Imported ${res.imported?.length ?? 0} file(s) into ${res.branch}`);
    location.reload();
  };

  // Python base64s the archive; we decode to a Blob and download through a
  // transient object URL. The date stamp is added here because main() has no
  // wall clock.
  const exportProfile = async (name: string) => {
    const res = await guard(cc.profiles.export(name));
    if (!res) return;
    if (!res.ok || !res.b64) {
      toastErr(res.error || "Export failed");
      return;
    }
    const bytes = Uint8Array.from(atob(res.b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${res.filename}-${new Date().toISOString().slice(0, 10)}.zip`;
    a.click();
    URL.revokeObjectURL(url);
    toastOk(`Exported ${res.filename}`);
  };

  const remove = async (name: string) => {
    const ok = await ask<boolean>({
      title: `Delete profile "${name}"?`,
      note: "The branch is deleted; git refuses if it holds changes no other profile has.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Delete", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.profiles.remove(name));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Delete failed");
      return;
    }
    toastOk(`Deleted ${name}`);
    reload();
  };

  return (
    <>
      {modal}
      {staged && (
        <ImportPicker
          entries={staged.entries}
          onCancel={() => setStaged(null)}
          onImport={runImport}
        />
      )}
      <Card>
        <CardTitle>New profile</CardTitle>
        <CardSub>
          Forks the current profile (<span className="cc-mono">{data?.current ?? "…"}</span>) and
          switches into it.
        </CardSub>
        <CardActions>
          <input
            className="field-control"
            aria-label="New profile name"
            placeholder="e.g. work, experiment"
            value={newName}
            disabled={busy}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button type="button" className="btn btn-primary" disabled={busy} onClick={create}>
            Create &amp; switch
          </button>
        </CardActions>
      </Card>
      <Card>
        <CardTitle>Import profile</CardTitle>
        <CardSub>
          Pick files/folders from an exported <span className="cc-mono">.zip</span> to overlay onto a
          new profile. Your current profile is untouched.
        </CardSub>
        <CardActions>
          <input
            className="field-control"
            type="file"
            accept=".zip"
            aria-label="Profile archive"
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Cleared so re-picking the same file fires change again.
              e.target.value = "";
              if (file) void pickZip(file);
            }}
          />
        </CardActions>
      </Card>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!data && !error && <SkeletonLines rows={3} label="Loading profiles" />}
      {data?.profiles.map((p) => (
        <Card key={p.name}>
          <CardTitle>
            {p.name} {p.current && <Pill tone="on">current</Pill>}{" "}
            {p.isDefault && <Pill>default</Pill>}
          </CardTitle>
          <CardActions>
            {!p.current && (
              <button type="button" className="btn" onClick={() => switchInto(p.name)}>
                Switch
              </button>
            )}
            <button type="button" className="btn" onClick={() => exportProfile(p.name)}>
              Export .zip
            </button>
            {!p.current && !p.isDefault && (
              <button type="button" className="btn btn-danger" onClick={() => remove(p.name)}>
                Delete
              </button>
            )}
          </CardActions>
        </Card>
      ))}
    </>
  );
}
