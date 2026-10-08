// Desktop notifications, the document.title prefix and tab-visibility bookkeeping (OpenBot chat.js).
import type { Bot, BotEvent } from "./api";
import { cur, eventsOf, getState, markSeen, needsYou, select, soundsOn, touch, unreadCount } from "../state/store";
import { handoverAsk } from "./derive";
import { botsMounted } from "./root";

export const NOTIFY_ROLES: Record<string, string> = { question: "asks you", approval: "needs approval", done: "finished", error: "hit an error" };
let notifyOk: boolean | null = null;

/** Ask for notification permission once (first click anywhere). */
export function askNotify(): void {
  if (notifyOk !== null || !("Notification" in window)) return;
  if (Notification.permission === "granted") { notifyOk = true; return; }
  if (Notification.permission === "denied") { notifyOk = false; return; }
  Notification.requestPermission().then((p) => { notifyOk = p === "granted"; }).catch(() => { notifyOk = false; });
}

/** Only for bots you are not looking at (or when this tab is hidden): the newest notify-worthy event of a poll. */
export function notifyEvents(bot: Bot, evs: BotEvent[]): void {
  if (bot.id === getState().sel && !document.hidden) return;
  const ev = evs.filter((e) => e.role in NOTIFY_ROLES).pop();
  if (!ev) return;
  if (notifyOk && "Notification" in window) {
    try {
      // A hand-off line (docs §11) is about the other bot: "<Bot> needs you at the laptop" / "<Bot> finished", not Super Bot.
      const who = ev.source === "handoff" && ev.handoff?.target_name ? ev.handoff.target_name : bot.name;
      const n = new Notification(`${who} ${NOTIFY_ROLES[ev.role]}`, { body: ev.text.slice(0, 160), tag: "bot-" + bot.id, silent: ev.role !== "question" });
      n.onclick = () => { window.focus(); select(bot.id); n.close(); };
    } catch { /* notifications unavailable */ }
  }
}

/** A bot asked for you in its browser (control_by "bot"; the store's poll diff): the OS notification, under the same
 *  rule as notifyEvents (a bot you are not looking at, or this tab hidden). Not silent: the hand-over is the alarm. */
export function notifyHandover(bot: Bot): void {
  if (bot.id === getState().sel && !document.hidden) return;
  if (!notifyOk || !("Notification" in window)) return;
  try {
    const n = new Notification(`${bot.name} needs you in the browser`, { body: (handoverAsk(bot, eventsOf(bot.id)) || "").slice(0, 160), tag: "bot-" + bot.id, silent: false });
    n.onclick = () => { window.focus(); select(bot.id); n.close(); };
  } catch { /* notifications unavailable */ }
}

let audio: AudioContext | null = null;
/** Two short sine notes (660 → 880 Hz) on a bot-initiated hand-over; no asset. Off with the Sounds pref. */
export function chime(): void {
  if (!soundsOn()) return;
  try {
    const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return;
    const ctx = audio || (audio = new AC());
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    const t0 = ctx.currentTime + 0.01;
    [660, 880].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain(), s = t0 + i * 0.12;
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, s);
      g.gain.exponentialRampToValueAtTime(0.15, s + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, s + 0.12);
      o.connect(g).connect(ctx.destination);
      o.start(s); o.stop(s + 0.13);
    });
  } catch { /* no audio */ }
}

// The title itself is the shell's (useDocumentTitle: "Bots – Fused Render"); this page only PREFIXES it, the way
// FusedBot's "? FusedBot" / "(n) FusedBot" did, and takes the prefix off again when the route unmounts.
const TITLE_PREFIX = /^(?:● |\? |\(\d+\) )/;
const baseTitle = (): string => document.title.replace(TITLE_PREFIX, "");

/** "● <title>" when a bot needs you in its browser, "? <title>" when another bot waits on you, "(n) <title>" for n bots with unread messages. */
export function updateTitle(): void {
  if (!botsMounted()) return;  // a poll that lands after the route unmounted must not prefix another page's title
  const S = getState();
  const n = S.bots.reduce((a, b) => a + (unreadCount(b) ? 1 : 0), 0);
  const q = S.bots.some((b) => b.status === "waiting" && b.id !== S.sel);
  const next = (S.bots.some(needsYou) ? "● " : q ? "? " : n ? `(${n}) ` : "") + baseTitle();
  if (next !== document.title) document.title = next;
}

/** Install the first-click permission ask and the visibility handler. Returns the teardown. */
export function installNotify(): () => void {
  const onVis = () => {
    const b = cur();
    // Coming back to the tab only updates the unread bookkeeping; the "new" rule stays where it was when you opened this bot.
    if (!document.hidden && b) markSeen(b.id, b.seq);
    touch();
  };
  document.addEventListener("click", askNotify, { once: true });
  document.addEventListener("visibilitychange", onVis);
  return () => {
    document.removeEventListener("click", askNotify);
    document.removeEventListener("visibilitychange", onVis);
    document.title = baseTitle();
  };
}
