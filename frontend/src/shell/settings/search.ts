// The Preferences search box. Every word typed must appear somewhere in a
// setting's label, description, keywords, section, group name or the nav
// heading over the group (so "claude code" finds every Claude Code row, as it
// did while they were one group of that name); results keep page order. The row (SettingRow) highlights the word in its label, and a
// result that only matched through a synonym says which word did — otherwise
// "tts" turning up Reader reads as a wrong answer.
import { groupOf, navSectionOf, type SettingEntry } from "./registry";

export interface LabelParts {
  pre: string;
  hit: string;
  post: string;
}

export function tokenize(query: string): string[] {
  return query.toLowerCase().split(/\s+/).filter(Boolean);
}

function haystack(e: SettingEntry): string {
  const group = groupOf(e.group);
  return [e.label, e.description, e.keywords, e.section, group.label, navSectionOf(group.nav).label]
    .join(" ")
    .toLowerCase();
}

export function splitLabel(label: string, tokens: readonly string[]): LabelParts {
  const lower = label.toLowerCase();
  const tok = tokens.find((t) => lower.includes(t));
  if (!tok) return { pre: label, hit: "", post: "" };
  const at = lower.indexOf(tok);
  return { pre: label.slice(0, at), hit: label.slice(at, at + tok.length), post: label.slice(at + tok.length) };
}

/** The keywords a result matched through, when what the reader can see —
 *  label and description — holds none of the query's words. */
export function matchedVia(entry: SettingEntry, tokens: readonly string[]): string[] {
  const plain = (entry.label + " " + entry.description).toLowerCase();
  if (tokens.some((t) => plain.includes(t))) return [];
  return entry.keywords
    .split(/\s+/)
    .filter((w) => tokens.some((t) => w.toLowerCase().includes(t)))
    .slice(0, 3);
}

/** The settings every word of `tokens` appears in, in page order. */
export function searchSettings<T extends SettingEntry>(entries: readonly T[], tokens: readonly string[]): T[] {
  if (!tokens.length) return [];
  return entries.filter((entry) => {
    const hay = haystack(entry);
    return tokens.every((t) => hay.includes(t));
  });
}
