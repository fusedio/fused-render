// What the two bot dialogs share (dialogs/CreateBot.tsx, dialogs/BotSettings.tsx): the model and effort menus, the
// app-folder normalisation the Trusted apps list matches on, and the Logins choice (which browser's sign-ins to share).
import type { Bot } from "./api";

export const MODELS: [string, string][] = [
  ["haiku", "Haiku · fastest"], ["sonnet", "Sonnet · balanced"], ["opus", "Opus · strongest"], ["fable", "Fable · most capable"],
  ["local-4b", "Gemma 4B · local model"], ["local-9b", "Gemma 12B · local model"],
];
/** Super Bot runs on Claude Code: no local models (the backend refuses them too). */
export const modelsFor = (isSuper: boolean): [string, string][] => (isSuper ? MODELS.filter(([v]) => !v.startsWith("local-")) : MODELS);

export const EFFORTS: [string, string][] = [["low", "Low · quickest"], ["medium", "Medium"], ["high", "High · careful"], ["xhigh", "Extra high · slowest"]];

/** Trusted apps: the backend stores folders normalised (lower, -/_/space → "-"); match the listing the same way so a
 *  saved entry ticks its row whatever the folder's spelling on disk. */
export const normApp = (f: string): string => f.trim().toLowerCase().replace(/[-_\s]+/g, "-");

/** A bot's browser id (its own id when the row predates shared browsers). */
export const browserOf = (b: Pick<Bot, "id" | "browser_id">): string => b.browser_id || b.id;

/** The Logins choice's other browsers: one per browser that other bots run on (Super Bot's included, so a new bot can
 *  share its Google sign-in), bots on one browser collapsed into one option, names in list order. `name` is the
 *  browser's display name (any bot's `browser_name`, else its first bot's name). `self` is left out of the names. */
export function loginGroups(bots: Bot[], self?: string): { id: string; name: string; names: string[] }[] {
  const out = new Map<string, { id: string; name: string; names: string[] }>();
  for (const b of bots) {
    if (b.id === self) continue;
    const k = browserOf(b), g = out.get(k) || { id: k, name: "", names: [] };
    g.names.push(b.name);
    if (!g.name && b.browser_name) g.name = b.browser_name;
    out.set(k, g);
  }
  return [...out.values()].map((g) => ({ ...g, name: g.name || g.names[0] || "" }));
}

/** A Logins option: "LinkedIn scout" when the browser carries its only bot's name, else
 *  "Super Bot · 2 bots · LinkedIn scout, Outreach" (the bot names only when they say more than the browser's). */
export function loginLabel(g: { name: string; names: string[] }): string {
  const n = g.names.length;
  if (n === 1 && g.names[0] === g.name) return g.name;
  const head = `${g.name} · ${n} bot${n === 1 ? "" : "s"}`;
  return g.names.every((x) => x === g.name) ? head : `${head} · ${g.names.join(", ")}`;
}

/** The settings value of a bot's Logins row: "" for a browser of its own (nobody else on it), else the shared
 *  browser's id. That id can be the bot's own: others joined ITS browser, so the row reads "Same as <them>". */
export const loginValue = (b: Pick<Bot, "id" | "browser_id" | "shared_with">): string => (b.shared_with?.length ? browserOf(b) : "");
