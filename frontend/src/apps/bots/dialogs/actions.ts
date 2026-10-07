// The bot-level flows behind the dialogs and the bot menu (OpenBot dialogs.js): create, save settings, delete,
// clone, export. Each runs its calls through act() so the page shows the effect and errors land in the banner.
import { api, type Bot, type Face } from "../lib/api";
import { act, cur, getState, select, setState } from "../state/store";
import { askConfirm } from "./ask";

/** What the bot dialogs hand back on OK (OpenBot botDialog's read()). CreateBot fills the fields it does not show
 *  with the first-run defaults (ask before risky things, scoped builds, own profile, no encryption). */
export interface BotDialogValue {
  name: string; model: string; effort: string; instructions: string; memory: string; approval: string; buildAccess: string;
  encrypt: boolean; profile: string; face: Face;
  /** Super Bot's phone (docs §10): the owner's handle, the Settings > Phone switch, the people it may text. Ignored
   *  for every other bot (the backend drops the keys on load and refuses them in Settings). */
  imessage: string;
  imessageEnabled: boolean;
  imessageTo: string;
  /** The preset key the bot was made from ("" for a blank bot, and always "" in Settings). */
  preset: string;
  /** "super" for Super Bot (docs §5), else "bot". */
  kind: string;
  /** Super Bot's Mac access: "ask" | "full" (ignored for other bots). */
  superAccess: string;
  /** App folders whose tools never ask this bot for approval (Settings > Permissions > Trusted apps). */
  trustedApps: string[];
}

/** The phone keys go only with Super Bot's settings: sending another bot's old value back (or "") would plant a key
 *  the backend drops on load (docs §10). */
const phone = (v: BotDialogValue): { imessage_handle?: string; imessage_enabled?: boolean; imessage_to?: string } =>
  (v.kind === "super" ? { imessage_handle: v.imessage, imessage_enabled: v.imessageEnabled, imessage_to: v.imessageTo } : {});

/** "+ New bot": create (with the preset, whose playbooks the backend copies), then the face; select it and drop
 *  focus into the composer. Everything else is a Settings matter once the bot exists. */
export async function createBot(v: BotDialogValue): Promise<void> {
  const r = await act(() => api.create({ name: v.name, model: v.model, effort: v.effort, instructions: v.instructions, approval: v.approval, build_access: v.buildAccess, encrypt: v.encrypt, preset: v.preset || "",
    kind: v.kind || "bot", super_access: v.superAccess }));
  const id = r?.id;
  if (id && v.trustedApps.length) await act(() => api.settings(id, { name: v.name, trusted_apps: v.trustedApps }));
  if (id && v.profile) await act(() => api.profile(id, v.profile));
  if (id && v.face && v.kind !== "super") await act(() => api.flag(id, { face: v.face }));  // Super Bot's mark is set by the backend
  if (id) select(id);
  document.getElementById("input")?.focus();
}

/** Settings → Save: the settings, then the face, then the Chrome profile import. A blank name saves nothing. */
export async function saveSettings(id: string, v: BotDialogValue): Promise<void> {
  if (!v.name) return;
  await act(() => api.settings(id, { name: v.name, model: v.model, effort: v.effort, instructions: v.instructions, memory: v.memory, approval: v.approval,
    build_access: v.buildAccess, encrypt: v.encrypt, ...phone(v), super_access: v.superAccess,
    trusted_apps: v.trustedApps }));
  if (v.face && v.kind !== "super") await act(() => api.flag(id, { face: v.face }));
  if (v.profile) await act(() => api.profile(id, v.profile));
}

/** Delete… (confirmed): drops the bot's events and cursor so nothing of it lingers, then deletes it. Defaults to the selected bot. */
export async function deleteBot(b: Bot | undefined = cur()): Promise<void> {
  if (!b) return;
  if (!(await askConfirm(`Delete "${b.name}"?`, "Its browser profile and history are removed."))) return;
  const s = getState(), events = { ...s.events }, cursors = { ...s.cursors };
  delete events[b.id]; delete cursors[b.id];
  setState({ events, cursors });
  await act(() => api.remove(b.id));
}

/** Clone: the copy is selected. */
export async function cloneBot(id: string | undefined = getState().sel || undefined): Promise<void> {
  if (!id) return;
  const r = await act(() => api.clone(id));
  if (r?.id) select(r.id);
}

/** Export transcript…: the thread as Markdown, downloaded. Defaults to the selected bot. */
export async function exportBot(b: Bot | undefined = cur()): Promise<void> {
  if (!b) return;
  const r = await act(() => api.exportTranscript(b.id), true);
  if (!r?.text) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([r.text], { type: "text/markdown" })); a.download = r.name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
