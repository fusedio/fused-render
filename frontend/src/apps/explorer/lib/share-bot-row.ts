// The "Share with bot…" row: the file goes to a bot's FILES folder with a
// message (shell/ShareBotModal, opened through platform/lib/share-bot). One
// definition for the two menus a file has — the preview's file menu (its `⋮`
// and the crumb bar's right-click, bar-menus' `share` group) and a listing
// row's menu (useFileOps.rowMenu) — and pure, like Preview's `shareRow`.
//
// ABSENT where the server runs no bots (Windows) and on a directory: a bot
// takes files, and a folder is granted to one in its settings instead.
import { createElement } from "react";
import { Bot } from "lucide-react";
import type { MenuEntry } from "@platform/ui/ContextMenu";

export function shareBotRow(args: { botsAvailable: boolean; isDir: boolean; onClick: () => void }): MenuEntry[] {
  if (!args.botsAvailable || args.isDir) return [];
  return [
    {
      label: "Share with bot…",
      // lucide at MenuIcons' own weight (1.5 on a 24 grid, 16px).
      icon: createElement(Bot, { size: 16, strokeWidth: 1.5, "aria-hidden": true }),
      title: "Send this file to a bot, with a message",
      onClick: args.onClick,
    },
  ];
}
