// Decision 2: the dropdown that completes path segments from disk beneath
// the merged field. `completionTarget` (pure) says which directory to list
// and what partial name narrows it; this hook is the debounced `listDir`
// behind that, keyed on the DIRECTORY rather than the whole query — a
// keystroke that only moves the partial (no new "/") narrows the same page
// client-side instead of re-listing, and a trailing "/" (a new directory)
// is what actually refreshes it, same as the spec asks for.
import { useEffect, useState } from "react";
import { listDir } from "@platform/lib/api";
import { INSTANT_DEBOUNCE_MS } from "@platform/lib/instant-search";
import {
  completionTarget,
  type CompletionTarget,
} from "@apps/explorer/listing/completion-target";

export interface CompletionItem {
  name: string;
  is_dir: boolean;
  size: number | null;
  // The value a Down/Enter taking this row writes back into the field: the
  // resolved path, with a trailing "/" on a directory so the dropdown moves
  // straight into it rather than closing on a segment that is not the query
  // yet, per useCompletion's own note above.
  path: string;
}

export interface Completion {
  // Null whenever there is nothing to show a header for (no dropdown at
  // all) — the same query shapes `completionTarget` itself returns null
  // for. Otherwise the directory `items` is a listing of, for the field's
  // "In ~/work/data" header (Listing.tsx formats the ~ substitution).
  target: CompletionTarget | null;
  items: CompletionItem[];
}

const MAX_ITEMS = 50;

export function useCompletion(
  query: string,
  fsPath: string,
  home: string | undefined,
): Completion {
  const target = completionTarget(query, fsPath, home);
  const [dir, setDir] = useState<string | null>(null);
  const [entries, setEntries] = useState<
    { name: string; is_dir: boolean; size: number | null }[]
  >([]);

  useEffect(() => {
    if (target === null) {
      setDir(null);
      setEntries([]);
      return;
    }
    if (target.dir === dir) return; // same directory, only the partial moved
    // listDir takes no AbortSignal, so a stale reply is dropped by this flag
    // rather than by cancelling the request itself.
    const mine = { current: true };
    const timer = window.setTimeout(() => {
      listDir(target.dir).then(
        (res) => {
          if (!mine.current) return;
          setDir(target.dir);
          setEntries(
            res.entries.map((e) => ({
              name: e.name,
              is_dir: e.is_dir,
              size: e.size,
            })),
          );
        },
        () => {
          if (!mine.current) return;
          setDir(target.dir);
          setEntries([]);
        },
      );
    }, INSTANT_DEBOUNCE_MS);
    return () => {
      mine.current = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.dir]);

  if (target === null || target.dir !== dir) return { target, items: [] };
  const partial = target.partial.toLowerCase();
  const base = target.dir.replace(/\/+$/, "");
  const items = entries
    .filter((e) => partial === "" || e.name.toLowerCase().startsWith(partial))
    .slice(0, MAX_ITEMS)
    .map((e) => ({
      name: e.name,
      is_dir: e.is_dir,
      size: e.size,
      path: base + "/" + e.name + (e.is_dir ? "/" : ""),
    }));
  return { target, items };
}
