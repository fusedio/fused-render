// Chip order for the Apps hub's Category facet. Categories are authored, one
// per app, in each app folder's metadata.json, so the set is whatever the
// workspace happens to contain — but a newcomer landing on /apps should meet
// the learning apps first, not whichever topic sorts earliest. So the chips
// lead with the learn-oriented categories in the order below and put
// everything else after them, locale-alphabetically.
//
// That tail order is deliberately NOT the previous one: the chips used to come
// out of a bare Array#sort, i.e. UTF-16 code-unit order, where "Zebra" sorts
// before "apple" because every capital does. These names are freehand author
// strings, so localeCompare's collation — case-insensitive-ish, and kinder to
// punctuation like the "-" in "local-ai" — is what a reader expects to see.

// Learn-oriented category names, in intended display order. Compared in
// normalized form (see normalize), so an author writing "How It Works" or
// "how_it_works" lands in the same slot as "howitworks".
const LEARN_ORDER = [
  "starters",
  "tutorials",
  "learn",
  "howitworks",
  "guides",
  "basics",
  "examples",
];

// Authored spellings vary: lower-case it and drop the separators authors reach
// for interchangeably (-, _, whitespace) so all of them rank alike.
const normalize = (c: string) => c.toLowerCase().replace(/[-_\s]+/g, "");

const RANKS = new Map(LEARN_ORDER.map((c, i) => [c, i]));

// Sort key: position in LEARN_ORDER, or one past the end for anything else —
// so no non-learn category can ever outrank a learn one, whatever its name.
export const learnRank = (category: string): number =>
  RANKS.get(normalize(category)) ?? LEARN_ORDER.length;

// Dedup + order a raw list of authored category names for the chip row. Learn
// rank first, then localeCompare — which decides both ties within a rank and
// the whole non-priority tail.
export function orderCategories(categories: string[]): string[] {
  return [...new Set(categories)].sort(
    (a, b) => learnRank(a) - learnRank(b) || a.localeCompare(b),
  );
}

// The Repo facet's chips. A tag is a SOURCE — the workspace tag dir an app was
// scanned out of, or `linked` for a folder registered from elsewhere — and an
// exported `.fused` (kind "appfile") has no source folder at all: its
// "Fused-App" tag names what the artifact IS. That belongs on the card's own
// tag line, which prints it, and not in a filter row that claims to group by
// where things came from — so appfile rows contribute no chip.
//
// Excluded by `kind`, not by matching the tag text, so rewording the tag
// cannot quietly put the chip back. The rows stay reachable: an explicit
// `?tag=` URL still filters to them (the All chip clears it) and the search
// box still matches their tag.
//
// Code-unit sort, unlike orderCategories: these are directory names, not
// freehand author strings, and the sort is the one the chips have always had.
export function repoChips(apps: { tag: string; kind?: string | null }[]): string[] {
  return [...new Set(apps.filter((a) => a.kind !== "appfile").map((a) => a.tag))].sort();
}
