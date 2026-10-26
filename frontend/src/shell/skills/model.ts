// Pure pieces of the Skills page: which installed skills a query matches, the
// `?skill=` key, and the page's URL params. No popularity anywhere: skills.sh
// rows keep the order the service returns them in and carry no count.
import { useEffect, useState } from "react";
import type { Skill, SkillRef } from "@apps/claude_config/api";

/** `<location>/<slug>` for a user skill, `plugin:<plugin>/<slug>` for a plugin's. */
export const skillKey = (s: SkillRef) =>
  s.source === "plugin" ? `plugin:${s.plugin}/${s.slug}` : `${s.location}/${s.slug}`;

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

/** A free name to offer when `name` is taken: name-2, name-3, … */
export function freeName(name: string, taken: (n: string) => boolean): string {
  for (let i = 2; i < 100; i++) if (!taken(`${name}-${i}`)) return `${name}-${i}`;
  return `${name}-copy`;
}
