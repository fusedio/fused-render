// The new-bot chooser's data (OpenBot dialogs.js pickPreset): the four blank starters, each card's searchable text,
// the search filter, Enter's pick, and what the bot dialog opens with for a pick. dialogs/PresetPicker.tsx draws it.
import type { Face, Preset } from "./api";

/** A blank starter: a named face, no playbooks. */
export interface Blank { name: string; face: Face }
// Four blank starters fill the first row: a named face each, no playbooks.
export const BLANKS: Blank[] = [
  { name: "Orange Bot", face: { shape: "cloud", color: "#f0762a" } },
  { name: "Pink Bot", face: { shape: "triangle", color: "#d33f8e" } },
  { name: "Blue Bot", face: { shape: "square", color: "#2f7ae5" } },
  { name: "Red Bot", face: { shape: "circle", color: "#d33b3b" } },
];

/** What the chooser resolves with: a preset, a blank starter, or Super Bot (one per Mac; offered only while none exists). */
export type NewBotPick = { kind: "preset"; preset: Preset } | { kind: "blank"; blank: Blank } | { kind: "super" };

/** Super Bot card (bot.py SUPER_FACE / SUPER_NAME): Claude Code's own tools on this Mac plus the browser. */
export const SUPER_NAME = "Super Bot";
export const SUPER_FACE: Face = { icon: "claude", color: "#262624" };  // the locked Claude mark (lib/face.ts BRANDS.claude)
export const SUPER_BLURB = "Runs on this Mac through your Claude Code login · files, shell, browser";
export const SUPER_NOTE = "Your assistant on this Mac. It runs locally through the Claude Code you are already signed in to (your Claude "
  + "subscription, nothing extra to set up) and gets Claude Code's tools (files, PDFs, images, shell, code) plus the browser. "
  + "Writes and commands ask you first; only your chat can give it tasks. One per Mac.";
export const superQ = "super bot assistant mac files shell claude code";

// Each card carries its searchable text (name, key, playbook titles); blank cards match the search on their name only.
export const presetQ = (p: Pick<Preset, "name" | "key" | "skills">): string => [p.name, p.key, ...(p.skills || [])].join(" ").toLowerCase();
export const blankQ = (b: Pick<Blank, "name">): string => `${b.name.toLowerCase()} blank`;

/** The search box's words (lower-cased, blanks dropped). */
export const queryWords = (query: string): string[] => query.toLowerCase().split(/\s+/).filter(Boolean);
/** A card shows when every word of the query is in its text. */
export const matchQ = (q: string, words: string[]): boolean => words.every((w) => q.includes(w));

/** One chooser card: `key` is the preset key, "" for a blank. */
export interface PickCard { key: string; q: string; pick: NewBotPick }
export const pickCards = (presets: Preset[], withSuper = false): PickCard[] => [
  ...(withSuper ? [{ key: "", q: superQ, pick: { kind: "super" } as NewBotPick }] : []),
  ...BLANKS.map((b): PickCard => ({ key: "", q: blankQ(b), pick: { kind: "blank", blank: b } })),
  ...presets.map((p): PickCard => ({ key: p.key, q: presetQ(p), pick: { kind: "preset", preset: p } })),
];

/** The cards the query leaves showing, and whether to show the "No preset matches" line. */
export function filterCards(cards: PickCard[], query: string): { shown: PickCard[]; none: boolean } {
  const words = queryWords(query), shown = cards.filter((c) => matchQ(c.q, words));
  return { shown, none: !!words.length && !shown.length };
}
/** One line of the search result list (the grid gives way to it while the box has text): a card itself (a site,
 *  a blank or Super Bot, matched on its name) or one playbook of a site whose title matched. `pick` is what a click
 *  opens; `title` / `sub` are the two texts shown; `key` is unique per row. */
export interface SearchRow { key: string; pick: NewBotPick; face: Face; name: string; title: string; sub: string; kind: "card" | "skill" }

/** Rows for `query`: cards whose VISIBLE name holds every word first (sites, blanks, Super Bot — in card order; the
 *  grid's hidden search text, with its "assistant mac shell" tokens, is not what a row can show lit), then one
 *  row per playbook whose title holds every word, in site order. A site matched only through its playbooks has no
 *  card row: its playbook rows are the reason it shows. [] for an empty query (the grid shows then). */
export function searchRows(cards: PickCard[], query: string): SearchRow[] {
  const words = queryWords(query);
  if (!words.length) return [];
  const own: SearchRow[] = [], skills: SearchRow[] = [];
  for (const c of cards) {
    if (c.pick.kind === "super") {
      if (matchQ(SUPER_NAME.toLowerCase(), words)) own.push({ key: "super", pick: c.pick, face: SUPER_FACE, name: SUPER_NAME, title: SUPER_NAME, sub: SUPER_BLURB, kind: "card" });
      continue;
    }
    if (c.pick.kind === "blank") {
      const b = c.pick.blank;
      if (matchQ(b.name.toLowerCase(), words)) own.push({ key: `blank:${b.name}`, pick: c.pick, face: b.face, name: b.name, title: b.name, sub: "From scratch", kind: "card" });
      continue;
    }
    const p = c.pick.preset, face: Face = { icon: p.key, color: p.color };
    if (matchQ(`${p.name} ${p.key}`.toLowerCase(), words)) own.push({ key: p.key, pick: c.pick, face, name: p.key, title: p.name, sub: `${p.skills.length} playbooks`, kind: "card" });
    for (const sk of p.skills || []) {
      if (matchQ(sk.toLowerCase(), words)) skills.push({ key: `${p.key}:${sk}`, pick: c.pick, face, name: p.key, title: sk, sub: p.name, kind: "skill" });
    }
  }
  return [...own, ...skills];
}

/** `text` split into plain and matched runs for highlighting: [[run, hit], ...]. Case-insensitive, every query word. */
export function highlightRuns(text: string, words: string[]): [string, boolean][] {
  if (!words.length || !text) return [[text, false]];
  const re = new RegExp(words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
  const out: [string, boolean][] = [];
  let i = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > i) out.push([text.slice(i, m.index), false]);
    out.push([m[0], true]);
    i = m.index! + m[0].length;
  }
  if (i < text.length) out.push([text.slice(i), false]);
  return out;
}

/** Enter in the search box picks the first preset still showing (not a blank, unless nothing else is left). */
export const firstPick = (shown: PickCard[]): PickCard | undefined => shown.find((c) => c.key) || shown[0];

/** "Comes with N playbooks: a, b. Edit them under Skills once the bot exists." for a preset bot's dialog. */
export const presetNote = (p: Pick<Preset, "skills">): string =>
  `Comes with ${p.skills.length} playbooks: ${p.skills.join(", ")}. Edit them under Skills once the bot exists.`;

/** What the bot dialog opens with for a pick (OpenBot's `$("add").onclick`). */
export function newBotInit(pick: NewBotPick): { title: string; name: string; model: string; instructions: string; face: Face; presetNote: string; preset: string; skills: string[] } {
  if (pick.kind === "preset") {
    const p = pick.preset;
    // The form lists `skills` itself (collapsed, "N playbooks"); presetNote stays the one-line fallback.
    return { title: `New ${p.name} bot`, name: `${p.name} bot`, model: p.model || "sonnet", instructions: p.instructions || "",
      face: { icon: p.key, color: p.color }, presetNote: presetNote(p), preset: p.key, skills: p.skills || [] };
  }
  if (pick.kind === "super") {
    // Instructions stay blank: the backend fills its standing rules in when the user types none.
    return { title: `New ${SUPER_NAME}`, name: SUPER_NAME, model: "sonnet", instructions: "", face: SUPER_FACE, presetNote: SUPER_NOTE, preset: "", skills: [] };
  }
  const b = pick.blank;
  return { title: `New ${b.name}`, name: b.name, model: "sonnet", instructions: "", face: b.face, presetNote: "", preset: "", skills: [] };
}
