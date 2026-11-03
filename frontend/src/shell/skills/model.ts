// Pure pieces of the Skills page: which installed skills a query matches, the
// `?skill=` key, and the page's URL params. No popularity anywhere: skills.sh
// rows keep the order the service returns them in and carry no count.
import { useEffect, useState } from "react";
import type { Skill, SkillRef } from "@apps/claude_config/api";

/** `<location>/<slug>` for a user skill, `plugin:<plugin>@<marketplace>/<slug>` for a plugin's. */
export const skillKey = (s: SkillRef) =>
  s.source === "plugin" ? `plugin:${s.plugin}@${s.marketplace ?? ""}/${s.slug}` : `${s.location}/${s.slug}`;

/** skills.sh is asked from this many letters on. */
export const REMOTE_MIN = 2;

/** The installed skills a query matches: name, slug, description and plugin. */
export function matchSkills(all: Skill[], query: string): Skill[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return all;
  return all.filter((s) =>
    `${s.name} ${s.slug} ${s.description} ${s.plugin ?? ""}`.toLowerCase().includes(needle),
  );
}

export interface PageParams {
  skill: string | null;
  newSkill: boolean;
  /** The description New skill opens with. */
  desc: string;
}

export function readParams(search: string): PageParams {
  const p = new URLSearchParams(search);
  return { skill: p.get("skill"), newSkill: p.get("new") === "1", desc: p.get("desc") ?? "" };
}

/** The page's URL for these params, other params kept. */
export function writeParams(search: string, next: Partial<PageParams>): string {
  const p = new URLSearchParams(search);
  const set = (k: string, v: string | null) => (v ? p.set(k, v) : p.delete(k));
  if ("skill" in next) set("skill", next.skill ?? null);
  if ("newSkill" in next) set("new", next.newSkill ? "1" : null);
  if ("desc" in next) set("desc", next.desc || null);
  const q = p.toString();
  return q ? `?${q}` : "";
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The task's title: the first line of the description, cut short. */
export function draftTitle(desc: string): string {
  const line = desc.trim().split("\n")[0].trim();
  return `Draft skill: ${line.length > 50 ? line.slice(0, 49).trimEnd() + "…" : line}`;
}

/** [frontmatter block, rest] of a SKILL.md, so the block can be tinted. */
export function splitFrontmatter(text: string): [string, string] {
  const m = /^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(text);
  return m ? [m[0], text.slice(m[0].length)] : ["", text];
}

/** SKILL.md with its frontmatter `name:` replaced; the rest is untouched. */
export function renameSkill(text: string, name: string): string {
  const [fm, rest] = splitFrontmatter(text);
  if (!fm) return `---\nname: ${name}\n---\n\n${text}`;
  const has = /^name:.*$/m.test(fm);
  const next = has
    ? fm.replace(/^name:.*$/m, `name: ${name}`)
    : fm.replace(/^---[ \t]*\r?\n/, `---\nname: ${name}\n`);
  return next + rest;
}

/** A skill name is at most this long (the backend's name limit). */
export const NAME_MAX = 64;

/** A free name to offer when `name` is taken: name-2, name-3, … Always within
 *  NAME_MAX: the base is cut to leave room for the suffix. */
export function freeName(name: string, taken: (n: string) => boolean): string {
  const cand = (suffix: string) => name.slice(0, NAME_MAX - suffix.length).replace(/[-_]+$/, "") + suffix;
  for (let i = 2; i < 100; i++) if (!taken(cand(`-${i}`))) return cand(`-${i}`);
  return cand("-copy");
}

/** "3.9k installs", "1 install", "1.2M installs"; "" for 0. */
export function formatInstalls(n: number): string {
  if (!(n > 0)) return "";
  const tenth = (v: number) => String(Math.round(v * 10) / 10);
  let s = String(n);
  if (n >= 1000) s = Math.round(n / 100) / 10 >= 1000 ? `${tenth(n / 1_000_000)}M` : `${tenth(n / 1000)}k`;
  return `${s} ${n === 1 ? "install" : "installs"}`;
}

/** Remote rows by installs, most first, the rows sharing a skillId folded
 * under the most-installed one: [{ top, rest }] in order of each top row. */
export function groupRemote<T extends { skillId: string; installs: number }>(rows: T[]): { top: T; rest: T[] }[] {
  const sorted = [...rows].sort((a, b) => b.installs - a.installs);
  const groups = new Map<string, { top: T; rest: T[] }>();
  for (const r of sorted) {
    const g = groups.get(r.skillId);
    if (g) g.rest.push(r);
    else groups.set(r.skillId, { top: r, rest: [] });
  }
  return [...groups.values()];
}

/** How well a remote row matches the query, lowest is best: 0 exact name,
 * 1 name starts with it or has it as a word, 2 name contains it, 3 the
 * source (owner/repo) contains it, 4 anything else. */
export function relevanceTier(row: { name: string; source: string }, q: string): number {
  const needle = q.trim().toLowerCase();
  if (!needle) return 4;
  const name = row.name.toLowerCase();
  if (name === needle) return 0;
  if (name.startsWith(needle) || name.split(/[-_\s]+/).includes(needle)) return 1;
  if (name.includes(needle)) return 2;
  if (row.source.toLowerCase().includes(needle)) return 3;
  return 4;
}

/** Groups ordered by the relevance tier of the top row, then by installs. */
export function rankRemote<T extends { name: string; source: string; installs: number }>(
  groups: { top: T; rest: T[] }[],
  q: string,
): { top: T; rest: T[] }[] {
  return groups
    .map((g) => ({ g, t: relevanceTier(g.top, q) }))
    .sort((a, b) => a.t - b.t || b.g.top.installs - a.g.top.installs)
    .map((x) => x.g);
}
