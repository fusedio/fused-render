// Decision 2: what the field's completion dropdown asks about. A path-shaped
// query names a directory to list and a partial name to narrow it by — this
// function is the pure split, the same address resolution `listing-address.ts`
// uses for decision 5, cut at the last "/" instead of requiring the whole
// thing to already be a real path. `useCompletion.ts` is the debounced
// `listDir` behind it.
//
// Only a query that already reads as a path gets a dropdown at all: a bare
// word with no "/" and no leading "~" is a plain filter (decision 3), and a
// glob is never a single directory to list (the same exclusions
// `listing-address.ts` makes for decision 5, and for the same reasons).
export interface CompletionTarget {
  dir: string;
  partial: string;
}

export function completionTarget(
  query: string,
  fsPath: string,
  home: string | undefined,
): CompletionTarget | null {
  const raw = query;
  if (!raw || raw.includes("*")) return null;
  if (!raw.includes("/") && raw !== "~") return null;

  // "~" alone has nothing after it to split on — the segment being completed
  // is everything home has to offer, not home's own last path component.
  if (raw === "~") {
    if (home === undefined) return null;
    return { dir: home, partial: "" };
  }

  let abs: string;
  if (raw.startsWith("~/")) {
    if (home === undefined) return null;
    abs = home + raw.slice(1);
  } else if (raw.startsWith("/")) {
    abs = raw;
  } else {
    // A relative query is scoped to the folder being searched — same as
    // listing-address.ts's `listingAddress`.
    abs = fsPath.replace(/\/+$/, "") + "/" + raw;
  }

  const slash = abs.lastIndexOf("/");
  const dir = slash <= 0 ? "/" : abs.slice(0, slash);
  const partial = abs.slice(slash + 1);
  return { dir, partial };
}

// The dropdown's "In ~/work/data" header — same ~ substitution PathCrumbs.tsx
// uses for the field itself, so the header names the directory the same way
// the rest of the field would once the field settled there.
export function displayDir(dir: string, home: string | undefined): string {
  if (home !== undefined && dir === home) return "~";
  if (home !== undefined && dir.startsWith(home + "/")) {
    return "~" + dir.slice(home.length);
  }
  return dir;
}

// A completion row's write-back value has to read as a continuation of what
// the user was already typing, not a rewrite into whatever notation
// happens to be convenient to compute. `absPath` is always absolute
// (`useCompletion.ts` builds it from `completionTarget`'s resolved `dir`);
// this re-derives whichever of the three notations `query` was actually
// typed in — tilde, absolute, or relative to `fsPath` — the same three-way
// split `completionTarget` above already makes when going the other
// direction. Reuses `displayDir`'s own `~` substitution rather than a
// second one, since the two need to agree on what counts as "under home".
// A trailing "/" on `absPath` (decision 2's "moves the dropdown into the
// folder" marker) survives untouched — every branch below is a slice off
// one end of the string, never a rebuild.
export function applyQueryNotation(
  absPath: string,
  query: string,
  fsPath: string,
  home: string | undefined,
): string {
  if (query === "~" || query.startsWith("~/")) {
    return displayDir(absPath, home);
  }
  if (query.startsWith("/")) {
    return absPath;
  }
  // Relative: typed against the folder being searched, same base
  // `completionTarget` resolves a relative query against.
  const base = fsPath.replace(/\/+$/, "");
  if (absPath === base) return "";
  if (absPath.startsWith(base + "/")) {
    return absPath.slice(base.length + 1);
  }
  return absPath;
}

// Decision 9: a dropdown holding exactly one row, whose name is already
// exactly what was typed, has nothing left to offer — the user finished
// typing that segment, and the row hands them back the text they just wrote.
// That panel only gets in the way: it sits over the "Press Enter to..."
// prompt the user is trying to read once the whole thing resolves to a real
// path. `items` is kept structural (just `name`) rather than importing
// `CompletionItem` from `useCompletion.ts`, so this stays a leaf the hook can
// depend on instead of the other way around.
export function isExactSingleMatch(
  items: { name: string }[],
  target: CompletionTarget | null,
): boolean {
  return items.length === 1 && target !== null && items[0].name === target.partial;
}
