// The uninstall confirm (AF-15): what goes, with the numbers (GET
// /api/apps/uninstall/preflight), then POST /api/apps/uninstall. The verb and
// the sentence follow the server's `kind`:
//
//   folder   → the app's files go. "Also delete the app's data" (on by
//              default) sends the WHOLE folder to the Trash (or, where the OS
//              has no bin, deletes it behind a second confirm); unticked, the
//              files and the git history are removed and `<app>/.fused/` is
//              left where it is, so a later install of the same app finds
//              its data again.
//   linked   → the folder stays; only the link goes
//   showcase → hidden from the hub; the showcase repo is left alone
import { useEffect, useRef, useState } from "react";
import {
  getUninstallPreflight,
  uninstallApp,
  type HttpError,
  type UninstallPreflight,
} from "@platform/lib/api";
import { formatBytes } from "@platform/lib/sysmon";
import { notify } from "@platform/lib/notifications";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { encodeFsPathSegments, navigateUrl } from "@platform/lib/router";
import { closeUninstallApp, useUninstallRequest, type UninstallRequest } from "@platform/lib/uninstall-app";
import { Modal } from "@platform/ui/modal/Modal";

function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function UninstallAppModal({ request }: { request: UninstallRequest }) {
  const { app } = request;
  const [pre, setPre] = useState<UninstallPreflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleteData, setDeleteData] = useState(true);
  // Flipped when the server answers 501 (no bin for this path): the same
  // button becomes "Delete forever" and the sentence says so.
  const [hardDelete, setHardDelete] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    getUninstallPreflight(app.path)
      .then((p) => {
        if (!live) return;
        setPre(p);
        if (p.kind === "folder" && !p.trash_supported) setHardDelete(true);
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [app.path]);

  const verb = !pre
    ? "…"
    : pre.kind === "linked"
      ? "Remove link"
      : pre.kind === "showcase"
        ? "Hide"
        : !deleteData
          ? "Remove app files"
          : hardDelete
            ? "Delete forever"
            : "Move to Trash";

  const confirm = async () => {
    if (!pre || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await uninstallApp(pre.path, {
        keep_data: pre.kind === "folder" && !deleteData,
        mode: hardDelete ? "delete" : "trash",
      });
      closeUninstallApp();
      announceCurrentAppsChanged();
      notify({
        title:
          pre.kind === "showcase"
            ? `${pre.name} hidden`
            : pre.kind === "linked"
              ? `${pre.name} removed from apps`
              : r.keep_data
                ? `${pre.name} removed, its data kept`
                : hardDelete
                  ? `${pre.name} deleted`
                  : `${pre.name} moved to Trash`,
        tone: "info",
      });
      // Standing on THIS app's page (/apps/<folder segments>, the same
      // encoding the app page uses): it no longer exists, go to the hub. Another app's page is
      // left alone.
      const here = location.pathname.replace(/\/+$/, "");
      const mine = "/apps/" + encodeFsPathSegments(pre.path).replace(/\/+$/, "");
      if (here === mine || here.startsWith(mine + "/")) navigateUrl("/apps");
    } catch (e) {
      const err = e as HttpError;
      if (err.status === 501) {
        setHardDelete(true);
        setError("No Trash is available for this folder. Delete it forever instead?");
      } else {
        setError(err.message);
      }
    } finally {
      setBusy(false);
    }
  };

  const title = pre ? `${verb}: ${pre.name}` : `Uninstall ${app.name}`;
  const lines: string[] = [];
  if (pre) {
    if (pre.kind === "linked") {
      lines.push(`${pre.path} is outside your workspace and stays where it is. It leaves the apps list.`);
    } else if (pre.kind === "showcase") {
      lines.push(`${pre.name} is part of the showcase checkout. It is hidden from the apps list; the files stay.`);
    } else if (deleteData) {
      lines.push(
        hardDelete
          ? `${pre.path} is deleted permanently, app data included. There is no undo.`
          : `${pre.path} moves to the Trash, app data included. Put it back from there to undo.`,
      );
    } else {
      lines.push(
        `The app's files and git history in ${pre.path} are removed. Its data stays in ${pre.path}/.fused and is found again when the app is installed next.`,
      );
    }
    if (pre.kind === "folder" && pre.uncommitted !== null && pre.uncommitted > 0) {
      lines.push(`${plural(pre.uncommitted, "change")} not yet committed to git go with it.`);
    }
    if (pre.tasks > 0) lines.push(`${plural(pre.tasks, "task")} under this folder will be archived.`);
    if (pre.bots.length > 0) lines.push(`Built by bots: ${pre.bots.join(", ")}. Those builds lose their folder.`);
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
            {busy ? "Removing…" : verb}
          </button>
        </>
      }
    >
      {!pre && !error && <p>Checking what this removes…</p>}
      {lines.map((l, i) => (
        <p key={i}>{l}</p>
      ))}
      {pre?.kind === "folder" && (
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            id="uninstall-delete-data"
            checked={deleteData}
            onChange={(e) => setDeleteData(e.target.checked)}
          />
          {pre.data_files > 0
            ? `Also delete the app's data (.fused: ${plural(pre.data_files, "file")}, ${formatBytes(pre.data_bytes)})`
            : "Also delete the app's data (.fused, empty)"}
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
