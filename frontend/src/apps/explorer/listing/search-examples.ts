// The focused teaching panel — this surface's own placeholder guidance,
// with the one thing a single placeholder line cannot teach: the contrast
// between a bounded glob and an unbounded one.
//
// SPEC-omnibox-search-affordance.md correction (2026-09-10): this used to
// gate on `query === ""`, back when an empty box was the only state a user
// staring at the placeholder could be in. It no longer is — the field
// always arrives pre-filled with the folder currently being searched (a
// hard requirement), so on focus the query is essentially NEVER empty, and
// this panel practically never rendered at all. `pristine` (query-
// pristine.ts's `isPristineQuery`) is the fix: empty OR still exactly the
// untouched pre-filled path, which is the box's actual resting state.
export interface SearchExample {
  pattern: string;
  hint: string;
}

/** The one shape this needs off a folder's own listed entries — a subset of
 * `FsEntry` (platform/lib/api.ts), so this stays a leaf neither
 * Listing.tsx's nor FileSearchField.tsx's own entry type needs importing
 * for. */
export interface ExampleEntry {
  name: string;
  is_dir: boolean;
}

// SPEC-omnibox-search-affordance.md correction (2026-09-10): a hardcoded
// "*.csv" / "~/work" example is a lesson that returns zero rows the moment
// it's pressed anywhere that isn't this machine's own ~/work — which
// teaches "search is broken", not the pattern syntax. Every example here
// has to be one the user standing in THIS folder could press and get real
// rows back for, derived from the SAME entries the listing below is already
// showing (no extra request this box has any business making).
//
// Slots 1 and 2 deliberately share one extension: the entire teaching value
// of the pair is that they differ by exactly one character
// ("*.csv" vs ".csv"), and a different extension in each would erase that.
// Slot 3's job is only to show that a search can start somewhere other than
// here — "~" is real on every machine and unambiguously elsewhere, so it
// needs no probing the way a literal folder name would.
//
// Every edge case here resolves the same way: show fewer examples, never an
// invented one — an empty return is a valid, honest answer, not the exception.
export function buildSearchExamples(
  entries: ExampleEntry[],
  fsPath: string,
  home: string | undefined,
): SearchExample[] {
  const counts = new Map<string, number>();
  for (const e of entries) {
    if (e.is_dir) continue;
    if (e.name.startsWith(".")) continue; // hidden entries don't vote
    const dot = e.name.lastIndexOf(".");
    if (dot <= 0) continue; // no extension (Makefile) — "." at position 0 is hidden, already excluded
    const ext = e.name.slice(dot + 1).toLowerCase();
    if (ext === "") continue;
    counts.set(ext, (counts.get(ext) ?? 0) + 1);
  }
  if (counts.size === 0) return [];

  // The most common extension wins; a tie breaks alphabetically so the
  // panel never reshuffles between renders of the same, unchanged folder.
  let winner = "";
  let winnerCount = -1;
  for (const [ext, count] of [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    if (count > winnerCount) {
      winner = ext;
      winnerCount = count;
    }
  }

  const EXT = winner.toUpperCase();
  const examples: SearchExample[] = [
    { pattern: `*.${winner}`, hint: `${EXT} files in this folder` },
    {
      pattern: `.${winner}`,
      hint: `${EXT} files in this folder and everything below it`,
    },
  ];
  // Omitted while standing in home itself — "searches from ~ instead of
  // here" is a false claim when here already IS ~. Trailing slash
  // tolerated the same way query-pristine.ts's own comparisons are.
  const stripSlash = (s: string) => s.replace(/\/+$/, "") || s;
  if (home === undefined || stripSlash(fsPath) !== stripSlash(home)) {
    examples.push({
      pattern: `~/*/*.${winner}`,
      hint: "searches from ~ instead of here",
    });
  }
  return examples;
}

/** Whether the examples panel should occupy the dropdown's surface.
 *
 * A pristine query resolves to a real, existing folder, so the completion
 * dropdown could legitimately have something to show for it too — but
 * that folder's own children are already listed in the rows directly
 * below, so duplicating that listing while teaching nothing is the worse
 * trade: PRISTINE WINS the surface over completions now, the opposite of
 * the old empty-query precedence (decided deliberately — do not
 * re-litigate). This function only ever answers for ITS OWN side of that
 * trade; the other half — completions never showing while pristine — is
 * SearchField.tsx's own `showCompletion` computation excluding `pristine`
 * before this function is ever asked, which is what keeps the two from
 * ever both being true rather than this function guessing at it. */
export function showSearchExamples(fieldActive: boolean, pristine: boolean): boolean {
  return fieldActive && pristine;
}
