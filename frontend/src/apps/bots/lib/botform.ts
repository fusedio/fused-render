// What the two bot dialogs share (dialogs/CreateBot.tsx, dialogs/BotSettings.tsx): the model and effort menus, the
// app-folder normalisation the Trusted apps list matches on, and the Logins choice (which bots' sign-ins to share).
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

/** The Logins choice's other browsers: one per browser that other ordinary bots run on (Super Bot never shares),
 *  bots on one browser collapsed into one option, names in list order. `self` is left out of the names. */
export function loginGroups(bots: Bot[], self?: string): { id: string; names: string[] }[] {
  const out = new Map<string, string[]>();
  for (const b of bots) {
    if (b.kind === "super" || b.id === self) continue;
    const k = browserOf(b);
    out.set(k, [...(out.get(k) || []), b.name]);
  }
  return [...out].map(([id, names]) => ({ id, names }));
}

/** The settings value of a bot's Logins row: "" for a browser of its own (nobody else on it), else the shared
 *  browser's id. That id can be the bot's own: others joined ITS browser, so the row reads "Same as <them>". */
export const loginValue = (b: Pick<Bot, "id" | "browser_id" | "shared_with">): string => (b.shared_with?.length ? browserOf(b) : "");
