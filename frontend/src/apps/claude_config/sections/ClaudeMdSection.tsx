// CLAUDE.md section: every CLAUDE.md-family file on the machine, grouped by
// directory, with preview / view-edit / reveal / delete per file.
//
// Discovery is the module's job (Spotlight ∪ the projects Claude Code has run
// in ∪ the global one). File CONTENT is NOT: it round-trips the shell's own
// filesystem API — `rawUrl` to read, `writeFile` to write — the same endpoints
// the explorer and the code editor use. There is deliberately no second file-IO
// path just for this panel.
//
// Save carries a mtime check rather than a true optimistic lock: /api/fs/write
// takes no expected-mtime, so the modal re-stats the file immediately before
// writing and refuses when the stamp moved since it was opened. That is a
// narrower guarantee than the original app's atomic lock (a write landing inside
// the stat→write window still wins) and it is stated here rather than dressed up
// — it catches the case that actually happens, which is a file edited in another
// window while this modal sat open.
import { useCallback, useState } from "react";
import { rawUrl, statPath, writeFile } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { ClaudeMdFile } from "../api";
import {
  Empty,
  Icon,
  Pill,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

interface Editing {
  path: string;
  text: string;
  // The stamp the file carried when it was opened (epoch seconds, as
  // /api/fs/stat reports it).
  mtime: number | null;
}

function ViewEditModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: Editing;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const editingNow = draft !== null;

  const save = async () => {
    if (draft === null) return;
    setBusy(true);
    try {
      const fresh = await statPath(editing.path);
      if (fresh.mtime !== editing.mtime) {
        toastErr("File changed on disk — reopen to reload");
        return;
      }
      await writeFile(editing.path, draft);
      toastOk("Saved");
      onSaved();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={editing.path.split("/").pop() || editing.path}
      width={620}
      busy={busy}
      dirty={editingNow && draft !== editing.text}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
          {editingNow ? (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>
              {busy ? "Saving…" : "Save"}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setDraft(editing.text)}
            >
              Edit
            </button>
          )}
        </>
      }
    >
      <div className="cc-card-sub cc-mono cc-modal-path">{editing.path}</div>
      {editingNow ? (
        <textarea
          className="cc-textbox cc-mono"
          aria-label="File contents"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
        />
      ) : (
        <pre className="cc-textbox cc-mono">
          {editing.text || <span className="cc-unset">(empty file)</span>}
        </pre>
      )}
    </Modal>
  );
}

export default function ClaudeMdSection({
  onChanged,
  preview,
  onPreview,
}: SectionProps & {
  // The split pane's current path (owned by the panel, since the pane renders
  // beside the section rather than inside it) and the setter that opens it.
  preview: string | null;
  onPreview: (path: string | null) => void;
}) {
  const load = useCallback(() => cc.claudeMd.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();
  const [editing, setEditing] = useState<Editing | null>(null);

  const open = async (file: ClaudeMdFile) => {
    try {
      const [st, res] = await Promise.all([statPath(file.path), fetch(rawUrl(file.path))]);
      if (!res.ok) throw new Error(`Could not read ${file.name} (HTTP ${res.status})`);
      setEditing({ path: file.path, text: await res.text(), mtime: st.mtime });
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  const remove = async (file: ClaudeMdFile) => {
    const ok = await ask<boolean>({
      title: "Delete this file?",
      preview: { files: [{ status: "D", path: file.path }], settings: [] },
      buttons: [
        { label: "Cancel", value: false },
        { label: "Delete", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.claudeMd.remove(file.path));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Delete failed");
      return;
    }
    // The pane was showing the file that no longer exists.
    if (preview === file.path) onPreview(null);
    toastOk("Deleted");
    onChanged();
    reload();
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={4} label="Loading CLAUDE.md files" />;
  if (!data.files.length) return <Empty>No CLAUDE.md files found.</Empty>;

  const emptyCount = data.files.filter((f) => f.empty).length;

  return (
    <>
      {modal}
      {editing && (
        <ViewEditModal
          editing={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onChanged();
            reload();
          }}
        />
      )}
      <div className="cc-toolbar">
        <span className="cc-unset">
          {data.files.length} file(s) via {data.engine}
          {emptyCount ? ` · ${emptyCount} empty` : ""}
        </span>
        <button type="button" className="btn" onClick={reload}>
          <Icon name="refresh" />
          Rescan
        </button>
      </div>
      {/* Compact grid, one small card per file — name + pills, its own path
          line, a few clamped lines of content (server-supplied snippet), and
          icon-only actions in the footer. The explorer homepage's bookmark
          cards are the visual reference; full-width cards made a 30-file scan
          a lot of scrolling for very little information. */}
      <div className="cc-mdgrid">
        {data.files.map((f) => (
          <div key={f.path} className="cc-mdcard">
            <div className="cc-mdcard-head">
              <span className="cc-mdcard-name cc-mono">{f.name}</span>
              {f.empty && <Pill tone="err">empty</Pill>}
              {f.name === "CLAUDE.local.md" && <Pill>local</Pill>}
              {f.scope === "global" && <Pill tone="on">global</Pill>}
            </div>
            <div className="cc-mdcard-path cc-mono" title={f.path}>
              {/* bdi keeps the path's characters in logical order inside the
                  rtl (left-ellipsizing) container — see .cc-mdcard-path. */}
              <bdi dir="ltr">{f.dir}</bdi>
            </div>
            {f.empty ? (
              <div className="cc-mdcard-snippet cc-unset">(empty file)</div>
            ) : (
              <pre className="cc-mdcard-snippet cc-mono">{f.snippet}</pre>
            )}
            <div className="cc-mdcard-foot">
              <span className="cc-mdcard-meta">
                {formatSize(f.size)} · {new Date(f.mtime * 1000).toLocaleDateString()}
              </span>
              <span className="cc-mdcard-actions">
                <button
                  type="button"
                  className="cc-iconbtn"
                  title="View / Edit"
                  aria-label={`View or edit ${f.path}`}
                  onClick={() => open(f)}
                >
                  <Icon name="edit" />
                </button>
                <button
                  type="button"
                  className={"cc-iconbtn" + (preview === f.path ? " cc-btn-on" : "")}
                  title="Preview"
                  aria-label={`Preview ${f.path}`}
                  aria-pressed={preview === f.path}
                  onClick={() => onPreview(preview === f.path ? null : f.path)}
                >
                  <Icon name="eye" />
                </button>
                <button
                  type="button"
                  className="cc-iconbtn"
                  title="Reveal in Finder"
                  aria-label={`Reveal ${f.path} in Finder`}
                  onClick={() => guard(cc.claudeMd.open(f.path))}
                >
                  <Icon name="folder" />
                </button>
                <button
                  type="button"
                  className="cc-iconbtn cc-iconbtn-danger"
                  title="Delete"
                  aria-label={`Delete ${f.path}`}
                  onClick={() => remove(f)}
                >
                  <Icon name="trash" />
                </button>
              </span>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
