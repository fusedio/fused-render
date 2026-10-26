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
