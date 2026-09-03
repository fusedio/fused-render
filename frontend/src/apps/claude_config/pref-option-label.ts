// How a catalog OPTION is said — pulled out of PreferencesSection.tsx so it can
// be unit-tested without dragging in that file's React/DOM import chain
// (../bits → the shell's router, which touches `location` at module init and
// has no place in a plain `bun:test` run). Pure functions, no side effects.
import type { PrefEntry } from "./api";

// How one catalog OPTION is said: its curated label if the entry has one, else
// the option's own spelling. See `optionLabels` in api.ts for why that map is
// sparse rather than exhaustive.
export function optionLabel(d: PrefEntry, o: string): string {
  return d.optionLabels?.[o] ?? o;
}

// A CONTEXT QUALIFIER on a model id: Claude Code spells the 1M-context variant
// of a model by suffixing it, `claude-fable-5-1[1m]`. The catalog lists a couple
// of these as options in their own right (`opus[1m]`), but it cannot list one
// for every id — the suffix is a modifier the CLI applies, not a separate model
// — so a value carrying one has to be understood rather than enumerated.
const QUALIFIER = /^(.+?)(\[[^\]]*\])$/;

// How the value ON DISK is said, which is not the same question as `optionLabel`:
// settings.json is not validated against our curated options, so this has to
// cope with a value the catalog does not list.
//
// Three answers, narrowest first:
//  1. a listed option — its label;
//  2. a listed option wearing a qualifier (`claude-fable-5-1[1m]`) — the SAME
//     entry's label with the suffix kept, because that is one model in a wider
//     context window, not an unknown one. It read as "(not in catalog)" before,
//     which told the user their own setting was unrecognised when it was the
//     catalog that had no row for a suffix;
//  3. anything else — itself, marked. Showing an unlisted value is the whole
//     point (see the option below): reporting the file beats contradicting it.
//
// In every branch the OPTION'S VALUE stays the raw string off disk, so choosing
// the row the user is already on is a no-op rather than a silent rewrite that
// drops their qualifier.
export function storedOptionLabel(d: PrefEntry, val: string): string {
  const listed = (o: string) => (d.options || []).includes(o);
  if (listed(val)) return optionLabel(d, val);
  const m = QUALIFIER.exec(val);
  if (m && listed(m[1])) return `${optionLabel(d, m[1])} ${m[2]}`;
  return `${val} (not in catalog)`;
}
