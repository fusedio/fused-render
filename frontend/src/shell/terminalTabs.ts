// The terminal drawer's tab model, kept free of React/xterm/network so every
// rule is directly testable (TerminalDrawer.test.tsx): the persisted shape and
// its migration, reconciling a cached list against the server's live list, and
// what the active tab becomes when one goes away.
//
// LABELS: a terminal created FOR a command is named after that program (first
// word of the command, path stripped: `claude --resume x` -> `claude`);
// otherwise after the shell the server reports (`zsh`), else "Terminal".
// Duplicates are left as they are (VS Code style) rather than numbered: a
// number derived from position would renumber the survivors every time a
// sibling closed. The tooltip carries the cwd to tell two `claude` tabs apart.

export const STORAGE_KEY = "fused-render:terminal-drawer";
export const MIN_HEIGHT = 120;
export const MAX_HEIGHT = 720;
export const DEFAULT_HEIGHT = 260;
export const DEFAULT_LABEL = "Terminal";

export interface TerminalTab {
  id: string;
  label: string;
  cwd?: string;
}

/** What survives a reload: the ordered ids, the active one, and each tab's
 * label/cwd (the server only knows the shell name, not which command a tab was
 * opened for). */
export interface DrawerState {
  height: number;
  sessionIds: string[];
  activeId: string | null;
  meta: Record<string, { label: string; cwd?: string }>;
}

export interface LiveSession {
  id: string;
  alive: boolean;
  shell?: string;
  cwd?: string;
}

/** The program a command starts, for a tab label: the first word, skipping
 * leading `VAR=value` assignments, with any directory part dropped. */
export function programLabel(command: string | undefined): string | null {
  if (!command) return null;
  for (const word of command.trim().split(/\s+/)) {
    if (word === "" || /^[A-Za-z_][A-Za-z0-9_]*=/.test(word)) continue;
    const base = word.replace(/^['"]|['"]$/g, "").split("/").filter(Boolean).pop();
    return base || null;
  }
  return null;
}

export function clampHeight(h: unknown): number {
  return typeof h === "number" && Number.isFinite(h)
    ? Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, h))
    : DEFAULT_HEIGHT;
}

/** Parse the persisted blob. The pre-multi-terminal shape `{height,
 * sessionId}` migrates to a one-element list; malformed input is the
 * default. */
export function parseState(raw: string | null): DrawerState {
  const fallback: DrawerState = { height: DEFAULT_HEIGHT, sessionIds: [], activeId: null, meta: {} };
  if (!raw) return fallback;
  try {
    const p = JSON.parse(raw) as Record<string, unknown>;
    if (!p || typeof p !== "object") return fallback;
    const height = clampHeight(p.height);
    let ids: string[] = [];
    if (Array.isArray(p.sessionIds)) {
      ids = p.sessionIds.filter((x): x is string => typeof x === "string" && x !== "");
      ids = ids.filter((x, i) => ids.indexOf(x) === i);
    } else if (typeof p.sessionId === "string" && p.sessionId !== "") {
      ids = [p.sessionId];
    }
    const activeId =
      typeof p.activeId === "string" && ids.includes(p.activeId)
        ? p.activeId
        : typeof p.sessionId === "string" && ids.includes(p.sessionId)
          ? p.sessionId
          : null;
    const meta: DrawerState["meta"] = {};
    if (p.meta && typeof p.meta === "object") {
      for (const [id, v] of Object.entries(p.meta as Record<string, unknown>)) {
        if (!ids.includes(id) || !v || typeof v !== "object") continue;
        const { label, cwd } = v as { label?: unknown; cwd?: unknown };
        if (typeof label === "string" && label) {
          meta[id] = { label, ...(typeof cwd === "string" && cwd ? { cwd } : {}) };
        }
      }
    }
    return { height, sessionIds: ids, activeId, meta };
  } catch {
    return fallback;
  }
}

export function stateFor(height: number, tabs: TerminalTab[], activeId: string | null): DrawerState {
  const meta: DrawerState["meta"] = {};
  for (const t of tabs) meta[t.id] = { label: t.label, ...(t.cwd ? { cwd: t.cwd } : {}) };
  return { height, sessionIds: tabs.map((t) => t.id), activeId, meta };
}

/** Verify every cached id against the server's live list: keep the alive ones
 * in their cached order, restore the cached active tab (else the first
 * survivor). Dead-but-listed sessions (the registry reaps lazily) are dropped. */
export function reconcileTabs(
  cached: DrawerState,
  live: LiveSession[],
): { tabs: TerminalTab[]; activeId: string | null } {
  const byId = new Map(live.filter((s) => s.alive).map((s) => [s.id, s]));
  const tabs: TerminalTab[] = [];
  for (const id of cached.sessionIds) {
    const row = byId.get(id);
    if (!row) continue;
    const m = cached.meta[id];
    const cwd = m?.cwd ?? row.cwd;
    tabs.push({ id, label: m?.label ?? (row.shell || DEFAULT_LABEL), ...(cwd ? { cwd } : {}) });
  }
  const activeId =
    cached.activeId !== null && tabs.some((t) => t.id === cached.activeId)
      ? cached.activeId
      : (tabs[0]?.id ?? null);
  return { tabs, activeId };
}

/** Remove one tab. If it was active, the neighbour to its right becomes active
 * (the one to its left when it was last); `empty` means nothing is left and
 * the caller should close the drawer. Removing an unknown id is a no-op. */
export function removeTab(
  tabs: TerminalTab[],
  activeId: string | null,
  id: string,
): { tabs: TerminalTab[]; activeId: string | null; empty: boolean } {
  const i = tabs.findIndex((t) => t.id === id);
  if (i === -1) return { tabs, activeId, empty: tabs.length === 0 };
  const next = tabs.filter((t) => t.id !== id);
  if (next.length === 0) return { tabs: next, activeId: null, empty: true };
  if (activeId !== id) return { tabs: next, activeId, empty: false };
  return { tabs: next, activeId: next[Math.min(i, next.length - 1)].id, empty: false };
}
