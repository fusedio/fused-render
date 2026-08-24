// Chip order for the Apps hub's Category facet. Categories are authored, one
// per app, in each app folder's metadata.json, so the set is whatever the
// workspace happens to contain — but the chip row is a curated shelf, not an
// index: a newcomer landing on /apps should meet the starter apps first and
// then the topics in the order the product wants them read, not in whichever
// order their names happen to collate.
//
// So CHIP_ORDER below is the authored running order, and anything a workspace
// turns up that is NOT in it goes after them, locale-alphabetically. That tail
// order is deliberately NOT a bare Array#sort, i.e. UTF-16 code-unit order,
// where "Zebra" sorts before "apple" because every capital does. These names
// are freehand author strings, so localeCompare's collation —
// case-insensitive-ish, and kinder to punctuation like the "-" in "local-ai" —
// is what a reader expects to see.

// The curated categories, in display order. Compared in normalized form (see
// normalize), so an author writing "Local AI" or "local_ai" lands in the same
// slot as "local-ai".
const CHIP_ORDER = ["starters", "local-ai", "productivity", "geospatial"];

// Authored spellings vary: lower-case it and drop the separators authors reach
// for interchangeably (-, _, whitespace) so all of them rank alike.
const normalize = (c: string) => c.toLowerCase().replace(/[-_\s]+/g, "");

// Keyed on the NORMALIZED name, so a hyphenated entry like "local-ai" is
// looked up under the same spelling normalize() produces for the authored
// category — key it verbatim and every multi-word entry silently misses.
const RANKS = new Map(CHIP_ORDER.map((c, i) => [normalize(c), i]));

// Sort key: position in CHIP_ORDER, or one past the end for anything else —
// so no uncurated category can ever outrank a curated one, whatever its name.
export const chipRank = (category: string): number =>
  RANKS.get(normalize(category)) ?? CHIP_ORDER.length;

// Dedup + order a raw list of authored category names for the chip row.
// Curated rank first, then localeCompare — which decides both ties within a
// rank and the whole uncurated tail.
export function orderCategories(categories: string[]): string[] {
  return [...new Set(categories)].sort(
    (a, b) => chipRank(a) - chipRank(b) || a.localeCompare(b),
  );
}

// The Folders facet's chips (mode key `repo`, label "Folders" — see MODES in
// Apps.tsx). A tag is a SOURCE — the workspace tag dir an app was scanned out
// of, or `linked` for a folder registered from elsewhere — and an exported
// `.fused` (kind "appfile") has no source folder at all: its
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
