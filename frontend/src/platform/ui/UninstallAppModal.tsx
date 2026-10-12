// The uninstall confirm: what goes, with the numbers (GET
// /api/apps/uninstall/preflight), then POST /api/apps/uninstall. The verb
// and the sentence follow the server's `kind`:
//
//   own-repo / monorepo / workspace  → the folder goes to the Trash (or, on a
//                                       platform without one, is deleted
//                                       behind a second confirm)
//   linked                            → the folder stays; only the link goes
//   appfile                           → the .fused leaves the hub; "Delete
//                                       file too" trashes it (same no-Trash
//                                       → delete-forever fallback)
//   showcase                          → hidden from the hub; the showcase
//                                       repo is left alone
//
// `.fused/data` is the one thing a delete loses for good (not in git, not
// in an export), so its size is the line the dialog leads with.
import { useEffect, useRef, useState } from "react";
import {
  getUninstallPreflight,
  uninstallApp,
  type HttpError,
  type UninstallMode,
  type UninstallPreflight,
} from "@platform/lib/api";
import { formatBytes } from "@platform/lib/sysmon";
import { notify } from "@platform/lib/notifications";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { navigateUrl } from "@platform/lib/router";
import { closeUninstallApp, useUninstallRequest, type UninstallRequest } from "@platform/lib/uninstall-app";
import { Modal } from "@platform/ui/modal/Modal";

function verb(kind: UninstallPreflight["kind"], mode: UninstallMode): string {
  if (mode === "delete") return "Delete forever";
  if (kind === "linked") return "Remove link";
  if (kind === "appfile") return mode === "delete_file" ? "Delete file" : "Remove from apps";
  if (kind === "showcase") return "Hide";
  return "Move to Trash";
}

function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function UninstallAppModal({ request }: { request: UninstallRequest }) {
  const { app } = request;
  const [pre, setPre] = useState<UninstallPreflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Flipped when the server answers 501 (no bin for this path): the same
  // button becomes "Delete forever" and the sentence says so.
  const [hardDelete, setHardDelete] = useState(false);
  const [deleteFileToo, setDeleteFileToo] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    getUninstallPreflight(app.path)
      .then((p) => {
        if (!live) return;
        setPre(p);
        if (p.kind !== "linked" && p.kind !== "showcase" && !p.trash_supported) {
          setHardDelete(true);
        }
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [app.path]);

  const mode: UninstallMode | undefined = !pre
    ? undefined
    : pre.kind === "appfile"
      ? deleteFileToo
        ? hardDelete
          ? "delete"
          : "delete_file"
        : "forget"
      : hardDelete
        ? "delete"
        : pre.mode;

  const confirm = async () => {
    if (!pre || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await uninstallApp(pre.path, mode);
      closeUninstallApp();
      announceCurrentAppsChanged();
      notify({
        title:
          r.mode === "trash"
            ? `${pre.name} moved to Trash`
            : r.mode === "delete" || r.mode === "delete_file"
              ? `${pre.name} deleted`
              : `${pre.name} removed from apps`,
        tone: "info",
      });
      // Standing on THIS app's page (/apps/<folder segments>): it no longer
      // exists, go to the hub. Another app's page is left alone.
      const here = decodeURIComponent(location.pathname).replace(/\/+$/, "");
      const mine = "/apps" + pre.path.replace(/^([A-Za-z]:)?/, "").replace(/\/+$/, "");
      if (here === mine || here.startsWith(mine + "/")) navigateUrl("/apps");
    } catch (e) {
      const err = e as HttpError;
      if (err.status === 501) {
        setHardDelete(true);
        setError(
          `No Trash is available for this ${pre.kind === "appfile" ? "file" : "folder"}. Delete it forever instead?`,
        );
      } else {
        setError(err.message);
      }
    } finally {
      setBusy(false);
    }
  };

  const title = pre ? `${verb(pre.kind, mode ?? pre.mode)}: ${pre.name}` : `Uninstall ${app.name}`;
  const lines: string[] = [];
  if (pre) {
    if (pre.kind === "linked") {
      lines.push(`${pre.path} is outside your workspace and stays where it is. It leaves the apps list.`);
    } else if (pre.kind === "showcase") {
      lines.push(`${pre.name} is part of the showcase checkout. It is hidden from the apps list; the files stay.`);
    } else if (pre.kind === "appfile") {
      lines.push(
        !deleteFileToo
          ? `${pre.path} leaves the apps list. The file stays where it is.`
          : hardDelete
            ? `${pre.path} is deleted permanently and leaves the apps list. There is no undo.`
            : `${pre.path} moves to the Trash and leaves the apps list.`,
      );
    } else {
      lines.push(
        hardDelete
          ? `${pre.path} is deleted permanently. There is no undo.`
          : `${pre.path} moves to the Trash. Put it back from there to undo.`,
      );
      if (pre.data_files > 0) {
        lines.push(
          `Its app data (.fused/data) holds ${plural(pre.data_files, "file")}, ${formatBytes(pre.data_bytes)}. That data exists nowhere else.`,
        );
      }
      if (pre.uncommitted !== null && pre.uncommitted > 0) {
        lines.push(`${plural(pre.uncommitted, "change")} not yet committed to git go with it.`);
      }
    }
    if (pre.tasks > 0) lines.push(`${plural(pre.tasks, "task")} under this folder will be archived.`);
    if (pre.bots.length > 0) lines.push(`Built by bots: ${pre.bots.join(", ")}. Those builds lose their folder.`);
    if (pre.dupes.length > 0) {
      lines.push(`Another copy of this app stays at ${pre.dupes.join(", ")}.`);
    }
  }

  return (
    <Modal
      title={title}
      busy={busy}
      onClose={closeUninstallApp}
      initialFocus={confirmRef}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={closeUninstallApp}>
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="btn btn-danger"
            aria-disabled={busy || !pre}
            onClick={confirm}
          >
            {busy ? "Removing…" : pre ? verb(pre.kind, mode ?? pre.mode) : "…"}
          </button>
        </>
      }
    >
      {!pre && !error && <p>Checking what this removes…</p>}
      {lines.map((l, i) => (
        <p key={i}>{l}</p>
      ))}
      {pre?.kind === "appfile" && (
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={deleteFileToo}
            onChange={(e) => setDeleteFileToo(e.target.checked)}
          />
          {hardDelete ? "Delete the .fused file too" : "Move the .fused file to the Trash too"}
        </label>
      )}
      {error && (
        <p className="deploy-error" role="alert">
          {error}
        </p>
      )}
    </Modal>
  );
}

export function UninstallAppHost() {
  const req = useUninstallRequest();
  if (!req) return null;
  return <UninstallAppModal key={req.seq} request={req} />;
}
