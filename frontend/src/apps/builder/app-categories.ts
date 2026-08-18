// Chip order for the Apps hub's Category facet. Categories are authored, one
// per app, in each app folder's metadata.json, so the set is whatever the
// workspace happens to contain — but a newcomer landing on /apps should meet
// the learning apps first, not whichever topic sorts earliest. So the chips
// lead with the learn-oriented categories in the order below and put
// everything else after them, alphabetically as before.

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
