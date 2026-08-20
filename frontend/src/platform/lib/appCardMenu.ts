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
import { downloadAppFile, revealPath, type AppInfo } from "./api";
import { openApp } from "./appEntry";
import { copyToClipboard } from "./clipboard";
import { navigate } from "./router";
import { pushToast } from "./toast";
import { MenuIcons } from "@platform/ui/MenuIcons";
import type { MenuEntry } from "@platform/ui/ContextMenu";

export function appCardMenu(app: AppInfo): MenuEntry[] {
  return [
    { label: "Open", icon: MenuIcons.open, onClick: () => openApp(app) },
    {
      label: "Open in Explorer",
      icon: MenuIcons.folder,
      // isDir is the nav hint the explorer uses to paint a listing scaffold
      // before the stat resolves — an app card always knows it has a folder.
      onClick: () => navigate(app.path, { isDir: true }),
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
    {
      // The whole app as one double-clickable `.fused` file (SPEC §43, D385).
      // Errors (not an app, over budget) come back
      // as a toast rather than a corrupt download — downloadAppFile throws.
      label: "Export App File",
      icon: MenuIcons.download,
      onClick: () => {
        downloadAppFile(app.path, app.name).catch((e: Error) =>
          pushToast({ msg: "Could not export " + app.name + ": " + e.message, tone: "error" }),
        );
      },
    },
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
