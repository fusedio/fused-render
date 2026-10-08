// The bot context menu's line icons and item list (OpenBot chat.js MI + openMenu's markup). components/BotMenu.tsx
// renders these; the action for each `a` is BotMenu's job (pin/hide → api.flag, read → markSeen, live → the live
// view, clone, settings/routines/skills/export/delete → dialogs).
import type { ReactNode } from "react";
import type { Bot } from "../lib/api";

const mi = (d: ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
);

export type MenuIcon = "live" | "settings" | "routines" | "skills" | "export" | "clone" | "pin" | "unpin" | "hide" | "unhide" | "read" | "delete";

export const MI: Record<MenuIcon, ReactNode> = {
  live: mi(<><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></>),
  settings: mi(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>),
  routines: mi(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>),
  skills: mi(<path d="m12 3 2.4 5.2 5.6.7-4.1 3.9 1 5.6L12 15.7l-4.9 2.7 1-5.6L4 8.9l5.6-.7z" />),
  export: mi(<><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" /></>),
  clone: mi(<><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>),
  pin: mi(<><path d="M12 17v5" /><path d="M9 3h6v6l2 3H7l2-3z" /></>),
  unpin: mi(<><path d="M12 17v5" /><path d="M9 3h6v6l2 3H7l2-3z" /><path d="m4 4 16 16" /></>),
  hide: mi(<><path d="M3 3l18 18" /><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" /><path d="M9.9 5.1A10.4 10.4 0 0 1 12 5c5 0 8.5 4 10 7a15 15 0 0 1-2.6 3.4" /><path d="M6.6 6.6C4.5 8 3 10 2 12c1.5 3 5 7 10 7 1.5 0 2.9-.4 4.1-1" /></>),
  unhide: mi(<><path d="M2 12c1.5-3 5-7 10-7s8.5 4 10 7c-1.5 3-5 7-10 7S3.5 15 2 12z" /><circle cx="12" cy="12" r="3" /></>),
  read: mi(<path d="m3 12 5 5L21 6" />),
  delete: mi(<><path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 13h10l1-13" /><path d="M9 7V4h6v3" /></>),
};

export type MenuAction = "live" | "settings" | "routines" | "skills" | "export" | "clone" | "cloneCopy" | "pin" | "hide" | "read" | "delete";
export type MenuItem = { a: MenuAction; label: string; icon: ReactNode; danger?: boolean } | "hr";

/** openMenu's rows, in order. full: the header ☰ menu (routines, skills, export, window); live: lead with "Open live view". */
export function menuItems(b: Bot, opts: { full?: boolean; live?: boolean } = {}): MenuItem[] {
  const { full, live } = opts, out: MenuItem[] = [], ea = b.kind === "super";
  if (live) out.push({ a: "live", label: "Open live view", icon: MI.live }, "hr");
  out.push({ a: "settings", label: "Settings…", icon: MI.settings });
  // Super Bot takes tasks from the chat only and there is one per Mac: no routines, no clone (the backend refuses both).
  if (full && !ea) out.push({ a: "routines", label: "Routines…", icon: MI.routines });
  if (full) out.push({ a: "skills", label: "Skills…", icon: MI.skills });
  if (full) out.push({ a: "export", label: "Export transcript…", icon: MI.export });
  // Clone shares the source's logins (one browser); the second row is the old behaviour, a copy of them.
  if (!ea) out.push({ a: "clone", label: "Clone", icon: MI.clone }, { a: "cloneCopy", label: "Clone with a copy of its logins", icon: MI.clone });
  out.push("hr",
    { a: "pin", label: b.pinned ? "Unpin" : "Pin to top", icon: b.pinned ? MI.unpin : MI.pin },
    { a: "hide", label: b.hidden ? "Unhide" : "Hide", icon: b.hidden ? MI.unhide : MI.hide },
    { a: "read", label: "Mark as read", icon: MI.read },
    "hr",
    { a: "delete", label: "Delete…", icon: MI.delete, danger: true });
  return out;
}
