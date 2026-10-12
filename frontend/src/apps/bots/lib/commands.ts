// Slash commands typed in the composer: "/clear" runs Bot.clear_context instead of sending a message. The popup opens
// as soon as the box holds a lone "/" word (nothing else typed: a command is the whole message, never mid-sentence),
// narrows as you type, and Enter / Tab / click runs the highlighted one. Text that matches no command ("/foo") is sent
// as an ordinary message: the bot may well be asked about a path.
// The catalog is not behind the Settings "Clear context button" switch: that switch governs the header button, this is
// the other entry point to the same route (store.ts clearContextOn).
import type { Bot, BotEvent } from "./api";
import { isNoise } from "./thread";

export interface Command {
  name: string;   // typed after the slash
  title: string;  // the popup's label
  hint: string;   // what it does, muted beside the label
}

export const COMMANDS: readonly Command[] = [
  { name: "clear", title: "Clear context", hint: "The bot starts fresh; this chat keeps the earlier messages, folded" },
];

/** The text after the slash when the whole box is one slash word ("/", "/cl", "/clear"); null otherwise (plain text,
 *  a slash mid-sentence, a second line). */
export const slashQuery = (text: string): string | null => {
  const m = /^\/(\S*)$/.exec(text);
  return m ? m[1] : null;
};

/** Commands whose name starts with the query; the exact name still matches, so the popup stays open on "/clear". */
export const matchCommands = (q: string): Command[] => COMMANDS.filter((c) => c.name.startsWith(q.toLowerCase()));

/** The command the box's text names exactly ("/clear", case-insensitive, surrounding space ignored), or null. */
export const commandFor = (text: string): Command | null => {
  const q = slashQuery(text.trim());
  return q === null ? null : COMMANDS.find((c) => c.name === q.toLowerCase()) || null;
};

/** Whether Clear context can run now, and the reason when it cannot (the header button's title and the popup's hint).
 *  Mirrors Bot.clear_context's guards (no task thread alive, browser handed back) plus "nothing to clear": the newest
 *  shown row is already the marker. `events` is the bot's feed (store eventsOf). The server stays the real guard. */
export const canClear = (b: Bot | undefined, events: BotEvent[]): { ok: boolean; why: string } => {
  if (!b) return { ok: false, why: "Clear context" };
  if (b.control) return { ok: false, why: "Hand the browser back first, then clear the context" };
  if (b.status !== "idle" && b.status !== "error") return { ok: false, why: "Stop the task first, then clear the context" };
  const shown = events.filter((e) => !isNoise(e)), last = shown[shown.length - 1];
  if (!last || last.source === "clear") return { ok: false, why: "Context is already clear" };
  return { ok: true, why: "" };
};
