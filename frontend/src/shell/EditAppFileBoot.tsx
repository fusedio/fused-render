// The in-shell half of `fused-render://open?file=` (SPEC §26 DL-7, D889):
// Render App's Edit button asked for a `.fused` to be opened for editing, and
// GET /clone redirected here with its path in `?_edit_appfile=`. Mounted once
// in the top document beside `UpdateNotifier` (same `!IS_EMBED` guard); it
// reads the param on first mount, strips it BEFORE any async work (so a
// reload mid-clone or a Back never re-runs the hand-off), then:
//
//   no local copy yet  → clone through the guarded POST /api/appfile/clone and
//                        move to the copy's entry page (the server sent us to
//                        Home for this case — a GET must not write, D3);
//   copy already there → the server already landed us ON that copy, so the
//                        app the user knows is on screen; ask over it whether
//                        to overwrite the copy with this .fused (the preview
//                        header's merge overwrite, D397) or keep it. Close
//                        keeps it — nothing the user edited is replaced
//                        without the Overwrite click.
//
// A modal over the running shell, not a page of its own: the owner's call on
// the gated /clone page this replaced.
import { useEffect, useLayoutEffect, useState } from "react";

import { ConfirmDialog } from "@apps/explorer/FsDialogs";
import {
  cloneAppFile,
  getAppEntry,
  getAppFileCloneTarget,
  overwriteAppFile,
  type AppFileCloneTarget,
} from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { currentUrl, navigateUrl, replaceSearch, viewUrlForFsPath } from "@platform/lib/router";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";

import { editAppFileFromSearch, withoutEditAppFile } from "./edit-appfile-lib";

// Read once, at module init, from the URL the redirect landed on — the same
// moment router.ts reads its own boot flags — so no later in-app navigation
// can be mistaken for a hand-off.
const BOOT_FILE: string | null =
  typeof location === "undefined" ? null : editAppFileFromSearch(location.search);

/** The copy's entry page when it declares one, else the folder — the rule the
 *  preview header's Clone button lands by (Preview.tsx `land`). */
async function copyViewUrl(dir: string): Promise<string> {
  try {
    const info = await getAppEntry(dir);
    if (info.entry) return viewUrlForFsPath(info.entry);
  } catch {
    /* no entry answer — the folder is still the right place */
  }
  return viewUrlForFsPath(dir);
}

export default function EditAppFileBoot() {
  const [ask, setAsk] = useState<{ file: string; target: AppFileCloneTarget } | null>(null);
  const [busy, setBusy] = useState(false);

  useLayoutEffect(() => {
    if (!ask) return;
    acquireOverlay();
    return () => releaseOverlay();
  }, [ask]);

  useEffect(() => {
    const file = BOOT_FILE;
    if (!file) return;
    // Strip first: whatever happens below, this URL must not replay it.
    replaceSearch(withoutEditAppFile(currentUrl()));
    let alive = true;
    (async () => {
      let target: AppFileCloneTarget;
      try {
        target = await getAppFileCloneTarget(file);
      } catch (e) {
        notify({ title: "Could not open " + basename(file) + " for editing: " + ((e as Error).message || "unreadable app file"), tone: "error" });
        return;
      }
      if (!alive) return;
      if (target.cloned) {
        // The server landed us on the copy already; the view underneath is
        // the app the user knows, and the question goes over it.
        setAsk({ file, target });
        return;
      }
      try {
        const r = await cloneAppFile(file);
        if (!alive) return;
        navigateUrl(await copyViewUrl(r.path), { isDir: false });
      } catch (e) {
        notify({ title: "Clone failed: " + ((e as Error).message || "unknown error"), tone: "error" });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (!ask) return null;
  const { file, target } = ask;
  const copyName = basename(target.path);
  return (
    <ConfirmDialog
      title={"A local copy of " + copyName + " already exists"}
      message={
        <>
          Overwrite <code>{target.path}</code> with the files in <code>{basename(file)}</code>?
          Your edits to those files are lost. <code>.venv</code>, <code>.fused</code> and any file
          the app file does not carry are kept. Close to keep working on your copy as it is.
        </>
      }
      confirmLabel={busy ? "Overwriting…" : "Overwrite"}
      danger
      onConfirm={() => {
        if (busy) return;
        setBusy(true);
        overwriteAppFile(file)
          .then(async (r) => {
            // The copy's files changed under the page that is showing them.
            // A hard reload is the honest refresh here: this is a boot-time
            // hand-off, nothing in a module-level store (a pending cut, …)
            // can exist yet, so router.ts's reload caution does not apply.
            location.assign(await copyViewUrl(r.path));
          })
          .catch((e) => {
            notify({ title: "Overwrite failed: " + ((e as Error).message || "unknown error"), tone: "error" });
            setBusy(false);
            setAsk(null);
          });
      }}
      onCancel={() => setAsk(null)}
    />
  );
}
