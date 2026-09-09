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
