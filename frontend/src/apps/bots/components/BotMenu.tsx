// The bot context menu (OpenBot chat.js openMenu, #cmenu) for the store's ui.menu request: right-click on a list row,
// or the preview header's ☰ (full + live, alignRight). Rows and icons come from menuIcons.tsx. Clamped into the window
// once laid out; closes on an outside click, Escape and window blur.
import { useEffect, useLayoutEffect, useRef } from "react";
import { cloneBot, deleteBot, exportBot } from "../dialogs/actions";
import { api, type Bot } from "../lib/api";
import { openLive } from "../lib/cdp";
import { act, botById, closeMenu, cur, getState, markSeen, openDialog, select, useBotsSelector } from "../state/store";
import { menuItems, type MenuAction } from "./menuIcons";

export { exportBot };


export function runMenuAction(a: MenuAction, b: Bot): void {
  const id = b.id;
  if (a === "pin") { void act(() => api.flag(id, { pinned: !b.pinned })); return; }
  if (a === "hide") { void act(() => api.flag(id, { hidden: !b.hidden })); return; }
  if (a === "read") { markSeen(id, b.seq); return; }
  if (a === "live") { openLive(id); return; }  // selects the bot, then OpenBot openFull()
  if (a === "clone") { void cloneBot(id); return; }
  // Escape hatch for what the mirror cannot do (file pickers, passkeys): the same profile as a real Chrome window. Relaunches Chrome, so it takes a few seconds.
  if (a === "window") { void act(() => api.window(id, !b.browser?.visible)); return; }
  select(id);
  if (a === "export") { void exportBot(cur()); return; }
  if (a === "delete") { void deleteBot(cur()); return; }
  openDialog({ kind: a, id });  // settings, routines, skills
}

export function BotMenu() {
  const menu = useBotsSelector((s) => s.ui.menu);
  const b = useBotsSelector((s) => (menu ? s.bots.find((x) => x.id === menu.id) : undefined));
  const ref = useRef<HTMLDivElement>(null);
  // Clamp into the window once laid out (alignRight: x is the menu's right edge, anchored under a header button).
  useLayoutEffect(() => {
    const m = ref.current; if (!m || !menu) return;
    const r = m.getBoundingClientRect(), left = menu.alignRight ? menu.x - r.width : menu.x;
    m.style.left = Math.max(8, Math.min(left, innerWidth - r.width - 8)) + "px";
    m.style.top = Math.min(menu.y, innerHeight - r.height - 8) + "px";
  }, [menu, !!b]);
  useEffect(() => {
    if (!menu) return;
    const onClick = (e: MouseEvent) => { if (!(e.target as Element).closest?.("#cmenu")) closeMenu(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeMenu(); };
    // The opening click (the ☰ button) must not close it on its way up: listen from the next tick.
    const t = setTimeout(() => document.addEventListener("click", onClick), 0);
    document.addEventListener("keydown", onKey); window.addEventListener("blur", closeMenu);
    return () => { clearTimeout(t); document.removeEventListener("click", onClick); document.removeEventListener("keydown", onKey); window.removeEventListener("blur", closeMenu); };
  }, [menu]);
  return (
    <div id="cmenu" ref={ref} className={`cmenu${menu && b ? " show" : ""}`}>
      {menu && b ? menuItems(b, menu).map((it, i) => it === "hr" ? <hr key={i} /> : (
        <button key={it.a} data-a={it.a} className={it.danger ? "danger" : undefined} onClick={() => {
          closeMenu();
          const t = botById(menu.id) || getState().bots.find((x) => x.id === menu.id); if (!t) return;
          runMenuAction(it.a, t);
        }}>{it.icon}{it.label}</button>
      )) : null}
    </div>
  );
}
