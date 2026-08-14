// The right-click menu on an app card, in macOS Finder order. Sibling of
// appEntry.ts and for the same reason: every surface that renders an app card
// should offer the same entries, and a builder that is a plain function of the
// AppInfo can be tested without a DOM.
//
// The entry this menu exists for is "Open in Explorer" — the app's FOLDER in
// fused-render's own explorer, as distinct from "Reveal in Finder", which hands
// the path to the OS file manager.
//
// It is no longer distinct from "Open" for the ordinary app (a folder with a
// page): a card opens that folder in the explorer now, so the two entries land
// in the same place. The entry stays anyway, because it is the only one that is
// ALWAYS the folder — an app whose entry is a lone non-page file opens that
// FILE (appEntry's `entry` branch), and "Open in Explorer" is how you get to the
// folder around it.
import { revealPath, type AppInfo } from "./api";
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
