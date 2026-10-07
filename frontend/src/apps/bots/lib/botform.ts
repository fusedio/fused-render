// What the two bot dialogs share (dialogs/CreateBot.tsx, dialogs/BotSettings.tsx): the model and effort menus, and
// the app-folder normalisation the Trusted apps list matches on.

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
