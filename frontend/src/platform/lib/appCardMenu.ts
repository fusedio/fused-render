// The right-click menu on an app card, in macOS Finder order. Sibling of
// appEntry.ts and for the same reason: every surface that renders an app card
// should offer the same entries, and a builder that is a plain function of the
// AppInfo can be tested without a DOM.
//
// The entry this menu exists for is "Open in Explorer" — the app's FOLDER in
// fused-render's own explorer, as distinct from "Reveal in Finder", which hands
// the path to the OS file manager.
//
// It is distinct from "Open" again, and for the ordinary app (a folder with a
// page) this is the entry that answers the other half of the card: "Open" lands
// on the app's entry PAGE (appEntry, D269), so this is the only entry that is
// ALWAYS the folder — the files around the page, which is where you go to edit
// the app rather than use it. For one release the two landed in the same place
// (D264's card opened the folder) and this entry was kept for the lone non-page
// `entry` case alone; it has its own job back.
import { revealPath, type AppInfo } from "./api";
import { openApp } from "./appEntry";
import { exportAppFile } from "./appShot";
import { copyToClipboard } from "./clipboard";
import { navigate } from "./router";
import { pushToast } from "./toast";
import { MenuIcons } from "@platform/ui/MenuIcons";
import type { MenuEntry } from "@platform/ui/ContextMenu";

// An exported `.fused` card's path is the FILE, not a folder (kind
// "appfile", D396): "Open in Explorer" lands on its CONTAINING folder — the
// files around it, same promise as the folder case — and "Export App File"
// is not offered (the card already IS the export). Canonical paths are
// forward-slashed on every platform (server's canonical_fs_path), so string
// dirname is exact here.
function containingDir(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut > 0 ? path.slice(0, cut) : "/";
}

export function appCardMenu(
  app: AppInfo,
  // The card's thumb element, when the opener has one: the export entry's
  // no-flash capture crop source (appShot, D396).
  captureEl?: Element | null,
): MenuEntry[] {
  const isAppFile = app.kind === "appfile";
  return [
    { label: "Open", icon: MenuIcons.open, onClick: () => openApp(app) },
    {
      label: "Open in Explorer",
      icon: MenuIcons.folder,
      // isDir is the nav hint the explorer uses to paint a listing scaffold
      // before the stat resolves — the folder is known either way: the app's
      // own for a folder-shaped app, the .fused file's parent for an export.
      onClick: () =>
        navigate(isAppFile ? containingDir(app.path) : app.path, { isDir: true }),
    },
    "separator",
    // Same label and glyph as the explorer's own row menu (listing/useFileOps),
    // so the two menus never name the same action differently.
    {
      label: "Reveal in Finder",
      icon: MenuIcons.reveal,
      onClick: () => {
        revealPath(app.path).catch((e: Error) =>
          pushToast({ msg: "Could not reveal " + app.name + ": " + e.message, tone: "error" }),
        );
      },
    },
    // The whole app as one double-clickable `.fused` file (SPEC §43, D385).
    // Errors (not an app, over budget) come back as a toast rather than a
    // corrupt download — downloadAppFile throws. Not offered on a card that
    // already IS a .fused file (the export route would 400 on a non-folder).
    ...(isAppFile
      ? []
      : ([
          {
            label: "Export App File",
            icon: MenuIcons.compress,
            onClick: () => {
              // exportAppFile also captures a tab screenshot into the file's
              // preview.png when the folder has no authored one (D396).
              exportAppFile(app, captureEl).catch((e: Error) =>
                pushToast({
                  msg: "Could not export " + app.name + ": " + e.message,
                  tone: "error",
                }),
              );
            },
          },
        ] satisfies MenuEntry[])),
    {
      label: "Copy Path",
      icon: MenuIcons.copyPath,
      // Confirm with a non-error toast; a failed write stays silent, as in the
      // explorer — the path is still reachable through Reveal in Finder.
      onClick: () => {
        copyToClipboard(app.path).then((ok) => {
          if (ok) pushToast({ msg: "Path copied", tone: "info" });
        });
      },
    },
  ];
}
