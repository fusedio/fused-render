// Skip-rule text <-> pattern-list conversion, and the "Restore defaults"
// logic — the pure half of Indexing.tsx.
//
// The editor is a textarea, one pattern per line, because that IS the
// server's own format: `clean_patterns` takes newline-separated text,
// comments and all (index/specs/scan-ignore.md §2). A row-per-chip widget
// would be a second, lossier representation of the same list.

export function patternsToText(patterns: string[]): string {
  return patterns.join("\n");
}

export function textToPatterns(text: string): string[] {
  return text.split("\n");
}

// A line that names a pattern, as opposed to a blank line or a `#` comment —
// mirrors clean_patterns' own comment/blank handling (scan-ignore.md §2)
// closely enough for the set-difference below, without mutating what is
// actually stored (comments and blank lines are preserved verbatim in the
// textarea; this is only used to decide what counts as "already present").
function isPatternLine(line: string): boolean {
  const t = line.trim();
  return t !== "" && !t.startsWith("#");
}

// Default patterns present in `defaults` but absent from `saved` — a set
// difference on the pattern strings, order-insensitive, so a user who
// reordered entries or added their own is not flagged as stale. `saved`
// is `config.ignore` (what is actually on disk), not the textarea's live
// text, so mid-edit typing never flips this.
export function missingDefaults(saved: string[], defaults: string[]): string[] {
  const have = new Set(saved.filter(isPatternLine).map((l) => l.trim()));
  return defaults.filter((d) => !have.has(d.trim()));
}

// The union merge "Restore defaults" performs: every line the user has,
// verbatim (comments, blanks and ordering untouched), with whatever default
// patterns are missing appended at the end. A no-op (same text back) when
// nothing is missing.
export function unionWithDefaults(text: string, defaults: string[]): string {
  const lines = textToPatterns(text);
  const have = new Set(lines.filter(isPatternLine).map((l) => l.trim()));
  const missing = defaults.filter((d) => !have.has(d.trim()));
  if (missing.length === 0) return text;
  // A single trailing "" is the split artifact of an empty textarea or a
  // trailing newline, not a blank line the user meant to keep — appending
  // straight after it would land the first default on its own blank line
  // (`textToPatterns("") === [""]`, `textToPatterns("a\n") === ["a", ""]`).
  // Drop only ONE: a genuine blank line the user typed still survives.
  const base =
    lines.length > 0 && lines[lines.length - 1] === "" ? lines.slice(0, -1) : lines;
  return [...base, ...missing].join("\n");
}
