// The bot-level flows behind the dialogs and the bot menu (OpenBot dialogs.js): create, save settings, delete,
// clone, export. Each runs its calls through act() so the page shows the effect and errors land in the banner.
import { api, type Bot, type Face } from "../lib/api";
import { act, cur, getState, select, setState } from "../state/store";
import { askConfirm } from "./ask";

/** What the bot dialog hands back on OK (OpenBot botDialog's read()). */
export interface BotDialogValue {
  name: string; model: string; effort: string; instructions: string; memory: string; approval: string; buildAccess: string;
  encrypt: boolean; profile: string; face: Face;
  /** The owner's phone handle; the dialog only shows it for Super Bot, the one bot reachable over iMessage (docs §10). */
  imessage: string;
  imessageTo: string;
  /** The preset key the bot was made from ("" for a blank bot, and always "" in Settings). */
  preset: string;
  /** "super" for Super Bot (docs §5), else "bot". */
  kind: string;
  /** Super Bot's Mac access: "ask" | "full" (ignored for other bots). */
  superAccess: string;
  /** App folders whose tools never ask this bot for approval (Settings → Advanced → Trusted apps). */
  trustedApps: string[];
}

/** The owner handle goes only with Super Bot's settings: the field is hidden for other bots, and sending their old
 *  value back (or "") would keep or wipe a key the router no longer reads (docs §10). */
const handle = (v: BotDialogValue): { imessage_handle?: string } => (v.kind === "super" ? { imessage_handle: v.imessage } : {});

/** "+ New bot": create (with the preset, whose playbooks the backend copies), then the iMessage fields, the Chrome
 *  profile and the face; select it and drop focus into the composer. */
export async function createBot(v: BotDialogValue): Promise<void> {
  const r = await act(() => api.create({ name: v.name, model: v.model, effort: v.effort, instructions: v.instructions, approval: v.approval, build_access: v.buildAccess, encrypt: v.encrypt, preset: v.preset || "",
    kind: v.kind || "bot", super_access: v.superAccess }));
  const id = r?.id;
  if (id && (v.imessage || v.imessageTo || v.trustedApps.length)) {
    await act(() => api.settings(id, { name: v.name, ...handle(v), imessage_to: v.imessageTo, trusted_apps: v.trustedApps }));
  }
  if (id && v.profile) await act(() => api.profile(id, v.profile));
  if (id && v.face && v.kind !== "super") await act(() => api.flag(id, { face: v.face }));  // Super Bot's mark is set by the backend
  if (id) select(id);
  document.getElementById("input")?.focus();
}

/** Settings → Save: the settings, then the face, then the Chrome profile import. A blank name saves nothing. */
export async function saveSettings(id: string, v: BotDialogValue): Promise<void> {
  if (!v.name) return;
  await act(() => api.settings(id, { name: v.name, model: v.model, effort: v.effort, instructions: v.instructions, memory: v.memory, approval: v.approval,
    build_access: v.buildAccess, encrypt: v.encrypt, ...handle(v), imessage_to: v.imessageTo, super_access: v.superAccess,
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
