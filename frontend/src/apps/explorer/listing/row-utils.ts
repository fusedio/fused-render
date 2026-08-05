// Small pure helpers over RowCtx batches, shared by the file operations and
// the context menus.
import { normDir, pruneDescendantPaths } from "@apps/explorer/lib/fs-actions";
import type { RowCtx } from "@apps/explorer/listing/types";

// Row-shaped pruneDescendantPaths, for the batch row ops (Trash, Delete): a
// search selection can hold a folder row and rows from inside it, and removing
// the folder already removes those. Same input order.
export function pruneDescendantRows(rows: RowCtx[]): RowCtx[] {
  const kept = new Set(pruneDescendantPaths(rows.map((r) => r.path)));
  return rows.filter((r) => kept.has(r.path));
}

// The target folder for a New File / Paste against a row: INTO a directory row,
// or the PARENT of a file row (Finder's behaviour).
export function targetDirOf(row: RowCtx): string {
  return normDir(row.isDir ? row.path : row.parentDir);
}

// Plural-friendly name for a batch of rows, used both in menu labels and as the
// `name` in a friendlyFsError context ("Couldn't duplicate \"3 items\"").
export function batchLabel(rows: { name: string }[]): string {
  return rows.length === 1 ? rows[0].name : `${rows.length} items`;
}
