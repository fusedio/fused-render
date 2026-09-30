// Bots page vocabulary: the fixed choices the dialog offers, the task status
// fold, the URL shape, and one tiny hook that watches the chat's own params.
// DOM-light on purpose (only the hook touches `location`).
import { useEffect, useState } from "react";
import type { DefaultModel, SessionEffort } from "@platform/lib/api";
import type { BotInput, BotTask } from "./api";

export const EMOJIS = [
  "🤖", "🦉", "🦊", "🐙", "🐝", "🦄", "🐧", "🐢",
  "🌱", "🌙", "⭐", "🔥", "⚡", "🌊", "🍀", "🎨",
  "📚", "🧭", "🧪", "🎧", "✈️", "🧠", "💡", "🚀",
];

// Bot accent colours. They reach the page ONLY as an inline `--bot-accent`
// style var (bot.json's own value), never as a stylesheet literal — the theme
// test forbids colour literals in styles/*.css.
export const COLORS = ["#7c5cff", "#2f81f7", "#1f9d74", "#e5a100", "#e5534b", "#d63fbd"];

export const MODELS: { value: DefaultModel; label: string }[] = [
  { value: "", label: "Default" },
  { value: "fable", label: "Fable" },
  { value: "opus", label: "Opus" },
  { value: "sonnet", label: "Sonnet" },
  { value: "haiku", label: "Haiku" },
];

export const EFFORTS: { value: SessionEffort; label: string }[] = [
  { value: "", label: "Default" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra high" },
  { value: "max", label: "Max" },
];

export const BLANK_BOT: BotInput = {
  name: "",
  persona: "",
  emoji: EMOJIS[0],
  color: COLORS[0],
  model: "",
  effort: "",
};

/** The empty-state chips: each prefills the New bot dialog. */
export const SAMPLE_BOTS: BotInput[] = [
  {
    ...BLANK_BOT,
    name: "Research buddy",
    emoji: "🦉",
    color: COLORS[0],
    persona:
      "You are a curious, rigorous research partner. You dig into questions with the user, " +
      "look things up on the web, cite sources, separate facts from guesses, and keep notes " +
      "of what the user is investigating.",
  },
  {
    ...BLANK_BOT,
    name: "Travel planner",
    emoji: "✈️",
    color: COLORS[1],
    persona:
      "You are an upbeat, detail-minded travel planner. You learn the user's travel style, " +
      "budget and constraints, suggest itineraries, and can have small trip-planning apps " +
      "built for them.",
  },
  {
    ...BLANK_BOT,
    name: "Product coach",
    emoji: "🧭",
    color: COLORS[2],
    persona:
      "You are a candid product coach. You help the user sharpen ideas into specs, ask the " +
      "hard questions about users and scope, and turn agreed ideas into apps by writing clear " +
      "specs for a builder.",
  },
];

/** Suggested openers under the bot hero; pressing one sends it as the first
 *  message of a fresh chat (ChatMount's `initialAsk`). */
export const OPENERS = [
  "What can you help me with?",
  "What do you remember about me?",
  "Help me plan a small app for something I do every week",
];

/** The persona's first sentence, for one-line spots (rail rows, top bar). */
export function personaLine(persona: string): string {
  const flat = persona.replace(/\s+/g, " ").trim();
  const m = /^(.{1,160}?[.!?])(\s|$)/.exec(flat);
  return m ? m[1] : flat;
}

export type TaskStatus = "starting" | "running" | "done" | "failed" | "cancelled" | "unknown";

/**
 * A bot task's status from the joined schedule entry: `state` says how far
 * the send got, `turn` how the turn ended ("" = still going). Unknown/absent
 * states read as "starting" — the entry may not be listed yet in the instant
 * after creation.
 */
export function taskStatus(t: Pick<BotTask, "state" | "turn">): TaskStatus {
  // No live entry (the server sends state "" when the schedule entry has
  // vanished): there is no status to claim, and "starting" would be a lie.
  if (!t.state) return "unknown";
  const state = t.state || "";
  const turn = t.turn || "";
  if (state === "cancelled" || turn === "cancelled") return "cancelled";
  if (state === "failed" || state === "error" || state === "missed" || turn === "failed" || turn === "error")
    return "failed";
  if (turn) return "done";
  if (state === "sent") return "running";
  return "starting";
}

/** `/bots?bot=<slug>` plus the fresh-chat seeds. The chat's own params
 *  (session_id, run, …) are deliberately NOT carried: they belong to the
 *  conversation that was on screen, and a different bot or a new chat is a
 *  different conversation. model/effort ride in the URL because the chat
 *  reads its params there (paramsSource="url"); ChatMount's model/effort props
 *  only seed a MEMORY store. */
export function botUrl(
  bot: { slug: string; model?: string; effort?: string },
  extra: Record<string, string> = {},
): string {
  const q = new URLSearchParams({ bot: bot.slug });
  if (!extra.session_id) {
    if (bot.model) q.set("model", bot.model);
    if (bot.effort) q.set("effort", bot.effort);
  }
  for (const [k, v] of Object.entries(extra)) if (v) q.set(k, v);
  return "/bots?" + q.toString();
}

/**
 * Chat params as they are on the URL right now. The chat writes `session_id` /
 * `run` with `history.replaceState`, which fires no event, so this polls the
 * query string (cheap: one string compare per tick) and re-renders only when a
 * watched value changes.
 */
export function useSearchParams(keys: readonly string[], epoch: number): Record<string, string> {
  const read = () => {
    const q = new URLSearchParams(location.search);
    const out: Record<string, string> = {};
    for (const k of keys) out[k] = q.get(k) ?? "";
    return out;
  };
  const [vals, setVals] = useState(read);
  const sig = keys.join(",");
  useEffect(() => {
    let last = JSON.stringify(read());
    setVals(JSON.parse(last));
    const tick = () => {
      const now = read();
      const s = JSON.stringify(now);
      if (s !== last) {
        last = s;
        setVals(now);
      }
    };
    const id = window.setInterval(tick, 500);
    window.addEventListener("popstate", tick);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("popstate", tick);
    };
    // `read` closes over `keys` only; `sig` is its identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, epoch]);
  return vals;
}
