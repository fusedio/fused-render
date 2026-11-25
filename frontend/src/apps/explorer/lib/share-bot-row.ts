// The "Share with bot…" row: the file goes to a bot's FILES folder with a
// message (shell/ShareBotModal, opened through platform/lib/share-bot). One
// definition for the two menus a file has — the preview's file menu (its `⋮`
// and the crumb bar's right-click, bar-menus' `share` group) and a listing
// row's menu (useFileOps.rowMenu) — and pure, like Preview's `shareRow`.
//
// A FILE OR A FOLDER: a file is copied to the bot's FILES folder; a folder is
// added to the folders the bot may read. The folder menu (bar-menus'
// `folderMenu`, its `share` group) carries the same row.
//
// ABSENT where the server runs no bots (Windows).
import { createElement } from "react";
import { Bot } from "lucide-react";
import type { MenuEntry } from "@platform/ui/ContextMenu";

export function shareBotRow(args: { botsAvailable: boolean; isDir: boolean; onClick: () => void }): MenuEntry[] {
  if (!args.botsAvailable) return [];
  return [
    {
      label: "Share with bot…",
      // lucide at MenuIcons' own weight (1.5 on a 24 grid, 16px).
      icon: createElement(Bot, { size: 16, strokeWidth: 1.5, "aria-hidden": true }),
      title: args.isDir ? "Let a bot read this folder, with a message" : "Send this file to a bot, with a message",
      onClick: args.onClick,
    },
  ];
}
