// The three text use cases, defined ONCE (SPEC AI-28b) — read by the Models
// page (its three headed sections) and the Playground (its switch, starters
// and thinking default) and the Home strip (its chips), so the label a person
// sees in one place is the label in the others.
//
// A use case is what the person is TRYING to do, not a property of a file:
// which model suits it is the catalog's answer (`AiCatalogModel.useCases` /
// `useCasePicks`, curated on the server), and this module owns only the
// words, the starter prompts and the thinking default. Vision is deliberately
// not one — it is a "sees images" badge on whichever rows have a vision tower.
//
// Imports nothing but a type. Home is eager and the Models page is lazy
// (App.tsx), so anything heavy pulled in here would land in the front-door
// bundle; that is also why a starter names its icon (`icon: "mail"`) and the
// Playground maps it to the glyph, rather than this file holding JSX.
import type { AiCatalogModel } from "@platform/lib/api";

export type UseCaseId = "writing" | "coding" | "reasoning";

export interface UseCaseStarter {
  name: string;
  /** The one-line subline — what the starter is, in a few words. */
  hint: string;
  /** A `StarterIcons` key (playground/starterIcons.tsx). */
  icon: "bulb" | "mail" | "bowl" | "code" | "list" | "plane" | "pen" | "chart";
  prompt: string;
}

export interface UseCase {
  id: UseCaseId;
  /** The Models page section heading. */
  label: string;
  /** The Playground switch — short enough for a segment. */
  shortLabel: string;
  /** The Home card's chip: one word. */
  chip: string;
  /** The one-line blurb under the section heading. */
  blurb: string;
  /** Does the Playground turn "Think first" on when this use case is chosen?
   *  A person can still flip it; this is the starting point only. */
  thinking: boolean;
  /** The composer's placeholder in the Playground. */
  placeholder: string;
  starters: UseCaseStarter[];
}

// Each starter is a real ask with its constraints spelled out — what to write,
// how long, what to leave out — the same authored style the eight originals
// were written in (D465). The first eight below are those originals, moved
// under the use case they belong to; the rest are new.
export const USE_CASES: UseCase[] = [
  {
    id: "writing",
    label: "Writing & everyday chat",
    shortLabel: "Writing & chat",
    chip: "Writing",
    blurb: "Emails, rewrites, quick questions",
    thinking: false,
    placeholder: "Ask anything, or paste something to rewrite…",
    starters: [
      {
        name: "How it guesses",
        hint: "an analogy, under 150 words",
        icon: "bulb",
        prompt:
          "Explain how a language model picks the next word to someone who has never written " +
          "code. Use one everyday analogy, stay under 150 words, and end with the thing people " +
          "most often get wrong about it.",
      },
      {
        name: "Decline a meeting",
        hint: "four warm sentences",
        icon: "mail",
        prompt:
          "Write a short, warm email declining Thursday's design review because I am shipping a " +
          "release that day. Offer to read the notes and send comments, keep it to four " +
          "sentences, and do not apologise twice.",
      },
      {
        name: "Dinner from this",
        hint: "three quick recipes",
        icon: "bowl",
        prompt:
          "I have rice, two eggs, spinach and a lemon. Give me three dinners I can cook in under " +
          "20 minutes — a title and three steps each, ordered from least to most effort.",
      },
      {
        name: "One day in Lisbon",
        hint: "walks and coffee",
        icon: "plane",
        prompt:
          "Plan one day in Lisbon for someone who would rather walk and drink coffee than queue " +
          "for museums. Morning, afternoon, evening — one line each, plus the walk between them.",
      },
      {
        name: "Three haiku",
        hint: "proud, tired, funny",
        icon: "pen",
        prompt:
          "Write three haiku about running a large AI model on a laptop that gets hot. Give each " +
          "a different mood: proud, tired, funny. Nothing about clouds.",
      },
    ],
  },
  {
    id: "coding",
    label: "Coding",
    shortLabel: "Coding",
    chip: "Coding",
    blurb: "Write, explain and fix code",
    thinking: false,
    placeholder: "Describe the code you need…",
    starters: [
      {
        name: "Explain an error",
        hint: "Python KeyError, three causes",
        icon: "code",
        prompt:
          "Explain what a Python KeyError means, the three most common ways it happens in real " +
          "code, and how to fix each one. One short snippet per fix, no preamble.",
      },
      {
        name: "Regex, in parts",
        hint: "ISO date, token by token",
        icon: "list",
        prompt:
          "Write a regular expression that matches an ISO date (YYYY-MM-DD) and nothing else, " +
          "then explain it token by token as a bullet list, including why each anchor is there.",
      },
      {
        name: "Write a function",
        hint: "with tests",
        icon: "code",
        prompt:
          "Write a Python function slugify(title) that lowercases, turns runs of anything that is " +
          "not a letter or digit into a single hyphen, and trims hyphens from both ends. Then " +
          "write five pytest cases, including an empty string and a title that is all symbols.",
      },
      {
        name: "Review this diff",
        hint: "paste a change",
        icon: "list",
        prompt:
          "Review the code change I paste below like a careful teammate. List only real problems " +
          "— bugs, missed edge cases, unclear names — most serious first, one line each with the " +
          "fix, and say plainly if it is fine to merge.\n\n",
      },
    ],
  },
  {
    id: "reasoning",
    label: "Deep reasoning",
    shortLabel: "Deep reasoning",
    chip: "Reasoning",
    blurb: "Hard problems, step by step",
    thinking: true,
    placeholder: "Describe the problem, with its constraints…",
    starters: [
      {
        name: "Argue both sides",
        hint: "16GB or 32GB, then commit",
        icon: "chart",
        prompt:
          "I am choosing between a laptop with 16GB of memory and one with 32GB for running AI " +
          "models locally. Argue both sides in a short table, then commit to one recommendation " +
          "and say what would change your mind.",
      },
      {
        name: "Plan a trip",
        hint: "budget and constraints",
        icon: "plane",
        prompt:
          "Plan four days in Japan for two people on 3,000 euros in total, flights excluded. We " +
          "hate early mornings, want one night in a ryokan, and will not change hotels more than " +
          "once. Give a day-by-day outline with a running budget, and say what you cut to fit.",
      },
      {
        name: "Word problem",
        hint: "show the steps",
        icon: "bulb",
        prompt:
          "A train leaves at 9:40 going 80 km/h. A second leaves the same station at 10:10 on " +
          "the same line going 110 km/h. When and where does the second catch the first? Show " +
          "each step, then check the answer a different way.",
      },
      {
        name: "Schedule puzzle",
        hint: "five tasks, two people",
        icon: "list",
        prompt:
          "Two people share one afternoon, 1pm to 6pm, and must finish five jobs: A takes 90 " +
          "minutes, B 60, C 45, D 120, E 30. D needs both of them for its whole length, and C " +
          "cannot start before B ends. Find a schedule that fits, or prove that none does.",
      },
    ],
  },
];

const BY_ID = new Map<string, UseCase>(USE_CASES.map((u) => [u.id, u]));

export function useCaseById(id: string | null | undefined): UseCase | null {
  return (id && BY_ID.get(id)) || null;
}

/** A URL or payload value as a use-case id, or null for anything unknown — a
 *  stale or hand-edited link opens the page rather than breaking it. */
export function parseUseCase(raw: string | null | undefined): UseCaseId | null {
  return useCaseById(raw)?.id ?? null;
}

/** The use cases a catalog entry carries, narrowed to the ids this client
 *  knows. An older server sends none, which reads as none. */
export function useCasesOf(entry: Pick<AiCatalogModel, "useCases">): UseCaseId[] {
  return (entry.useCases ?? []).filter((id): id is UseCaseId => BY_ID.has(id));
}

/** Does this model think before it answers (the server's `thinks` tag)? */
export function modelThinks(entry: Pick<AiCatalogModel, "tags">): boolean {
  return !!entry.tags?.includes("thinks");
}

/** The model to start a use case on: the server's starred pick, else the first
 *  model that belongs to it. Null when nothing does — never a stranger, so the
 *  caller keeps whatever it already had. */
export function pickForUseCase(models: AiCatalogModel[], id: UseCaseId): AiCatalogModel | null {
  const starred = models.find((m) => m.useCasePicks?.includes(id));
  if (starred) return starred;
  return models.find((m) => useCasesOf(m).includes(id)) ?? null;
}

/** The closest CURATED model that can see images and fits this machine, for the
 *  Playground's "this model can't see images" notice. Closest by download size
 *  to the model being left: the swap should feel like the same tier of model,
 *  not a jump to the biggest or the smallest. Null when none qualifies — the
 *  notice then names no model rather than one that would not run. */
export function alternativeThatSees(
  models: AiCatalogModel[],
  current: AiCatalogModel,
): AiCatalogModel | null {
  const here = current.size_gb ?? 0;
  const candidates = models.filter(
    (m) =>
      m.id !== current.id &&
      m.source === "curated" &&
      m.acceptsImage &&
      m.fit?.verdict !== "no",
  );
  const gap = (m: AiCatalogModel) => (m.size_gb == null ? Infinity : Math.abs(m.size_gb - here));
  candidates.sort((a, b) => gap(a) - gap(b));
  return candidates[0] ?? null;
}
