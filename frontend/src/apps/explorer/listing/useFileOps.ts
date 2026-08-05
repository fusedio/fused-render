// File operations on listing rows (paste, duplicate, compress, trash, delete,
// rename, new file/folder, reveal, copy-path, open-in-Claude) plus the context
// menus built from them. Owns the dialog + context-menu state; toasts go to
// the global store (lib/toast) and the cut/copy clipboard is the module-level
// store in lib/fs-clipboard (both survive this component's per-folder remount).
import { useRef, useState } from "react";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import {
  writeFile,
  mkdir,
  deleteEntry,
  renameEntry,
  copyEntry,
  compressEntry,
  gitRepoInfo,
  statPath,
  revealPath,
} from "@platform/lib/api";
import type { ArchiveFormat } from "@platform/lib/api";
import {
  normDir,
  join,
  freeArchivePath,
  freeDuplicatePath,
  freePastePath,
  copyToClipboard,
  notePathDeleted,
  remapClipboardPath,
  pruneDescendantPaths,
  trashEntry,
  resolveOpenWithModes,
  buildOpenWithItems,
  buildCompressItems,
  friendlyFsError,
  claudeDeepLink,
} from "@apps/explorer/lib/fs-actions";
import { basename } from "@platform/lib/format";
import { isAppEntry } from "@platform/ui/FileIcons";
import { getClipboard, setClipboard, type Clipboard } from "@apps/explorer/lib/fs-clipboard";
import { pushToast } from "@platform/lib/toast";
import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { nameError } from "@apps/explorer/FsDialogs";
import type { DialogState, RowCtx } from "@apps/explorer/listing/types";
import { pruneDescendantRows, targetDirOf, batchLabel } from "@apps/explorer/listing/row-utils";

export function useFileOps({
  base,
  clipboard,
  refetch,
  pendingSelectRef,
}: {
  base: string;
  clipboard: Clipboard | null;
  refetch: () => void;
  // A path the selection should jump to once it appears in the reloaded rows
  // (a rename/duplicate target — its row doesn't exist until the refetch lands).
  pendingSelectRef: React.MutableRefObject<string | null>;
}) {
  // The open context menu (position + items) and the open modal, both local to
  // this folder view.
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);

  // Run a mutating fs call, then refetch on success or surface its error as a
  // toast. The dir-watch socket also refetches, but that lags 300 ms and only
  // fires for the listed dir — an explicit refetch keeps the UI immediate.
  // `ctx` ({verb, name}) is optional but supplied by every menu action, so the
  // caught wire string is humanized (friendlyFsError) instead of leaking bare.
  const run = async (fn: () => Promise<unknown>, ctx?: { verb: string; name: string }) => {
    try {
      await fn();
      refetch();
    } catch (e) {
      pushToast({ msg: ctx ? friendlyFsError(e, ctx) : (e as Error).message, tone: "error" });
    }
  };

  // Belt-and-braces name guard for the New File / New Folder / Rename handlers:
  // the dialog already blocks invalid names, but re-check here (and toast) before
  // building a path so a "." / ".." / separator can never escape the folder.
  // Returns true when the name is rejected (caller should bail).
  const rejectName = (name: string): boolean => {
    const err = nameError(name);
    if (err) pushToast({ msg: err, tone: "error" });
    return err !== null;
  };

  // Guards a paste that's still running so a second Paste gesture (a rapid
  // Cmd+V×2) can't fire a parallel op on the same source — for a cut that
  // second call would renameEntry an already-moved src and 404 with a jarring
  // toast. Reset in the flight's .finally, so sequential copy-pastes stay fine.
  const pasteInFlight = useRef(false);

  // Paste into `dir`: a cut moves (rename) and clears the clipboard; a copy
  // duplicates and keeps it. Same basename in the target folder either way.
  // The TARGET is always a single folder; the SOURCE may be several paths (a
  // multi-row cut/copy), which are processed in order — sequentially, because
  // freePastePath resolves a name against a listing and parallel calls would
  // both pick the same free "… copy" name.
  // Reads the clipboard synchronously (getClipboard) and consumes a cut BEFORE
  // the await, so re-entry sees an empty clipboard and no-ops.
  const doPaste = (dir: string) => {
    const clip = getClipboard();
    if (!clip || clip.paths.length === 0 || pasteInFlight.current) return;
    const target = normDir(dir); // "" (root) → "/", and join avoids "//name"
    const { op } = clip;
    // A clipboard filled from search results can hold a folder AND entries
    // inside it (the hit list is a flat recursive walk). Paste the outermost
    // ancestors only: the folder's move/copy carries its contents, so a
    // descendant entry would either 404 on a source the parent already moved
    // (killing the rest of the batch) or, for a copy, drop a stray second copy
    // of the inner entry at the top of the target.
    const paths = pruneDescendantPaths(clip.paths);
    const label = paths.length === 1 ? basename(paths[0]) : `${paths.length} items`;
    if (op === "cut") setClipboard(null); // consume atomically, before any await
    pasteInFlight.current = true;
    run(async () => {
      const pasted: string[] = [];
      let last: string | null = null;
      try {
        for (const src of paths) {
          // Same-folder paste (dst would collide with the source), matching Finder:
          //   • CUT into its own folder is a no-op — the backend rename would 409
          //     on dst === src, so skip it (the clipboard is already cleared).
          //   • COPY into its own folder makes a deduped "… copy" instead of
          //     colliding (freeDuplicatePath, same as Duplicate).
          const sameFolder = join(target, basename(src)) === src;
          if (sameFolder && op === "cut") {
            pasted.push(src);
            continue;
          }
          // Both ops keep the name when free and dedupe to "… copy" when taken
          // (Finder keep-both), instead of surfacing a 409.
          const { is_dir } = await statPath(src);
          const dst = sameFolder
            ? await freeDuplicatePath(target, basename(src), is_dir)
            : await freePastePath(target, basename(src), is_dir);
          if (op === "cut") await renameEntry(src, dst);
          else await copyEntry(src, dst);
          pasted.push(src);
          last = dst;
        }
      } catch (e) {
        // The paste failed (e.g. a 403, or the source vanished); for a cut the
        // pre-clear above dropped the clipboard, so re-set the cut for whatever
        // hasn't moved yet and let run() toast the error — without this the user
        // would have to re-cut before retrying. Skip the restore if the user
        // cut/copied something newer mid-flight.
        // Restoring from the PRUNED list (not clip.paths) is what keeps the
        // retry viable: a descendant of an already-moved folder is gone from
        // its old location, so putting it back on the clipboard would make
        // every retry fail on the same dead source.
        if (op === "cut" && getClipboard() === null) {
          const left = paths.filter((p) => !pasted.includes(p));
          if (left.length) setClipboard({ paths: left, op: "cut" });
        }
        // A multi-path paste can move/copy some entries before throwing. run()
        // only refetches when the whole callback resolves, so refresh here or
        // the listing keeps showing rows that are already gone (or misses the
        // ones already written) until the 300 ms dir-watch catches up. The
        // rethrow is preserved so run() still toasts the failure.
        if (pasted.length) refetch();
        throw e;
      }
      // Re-anchor onto the last thing written, if it lands in this view.
      if (last !== null) pendingSelectRef.current = last;
    }, { verb: "paste", name: label }).finally(() => {
      pasteInFlight.current = false;
    });
  };

  // Duplicate into the same folder, picking the first free "… copy[/ n]" name
  // (freeDuplicatePath lists the folder so the copy never 409s on an existing
  // name).
  // In-flight guard, same idea as pasteInFlight: a rapid double Cmd+D would
  // race both calls to the same free "… copy" name and 409 the second.
  // Acts on the whole selection; the rows are duplicated one at a time for the
  // same reason paste is sequential (each freeDuplicatePath re-reads the folder,
  // so parallel calls would pick colliding names).
  const duplicateInFlight = useRef(false);
  const doDuplicate = (rows: RowCtx[]) => {
    if (!rows.length || duplicateInFlight.current) return;
    duplicateInFlight.current = true;
    run(async () => {
      let last: string | null = null;
      try {
        for (const row of rows) {
          const dst = await freeDuplicatePath(row.parentDir, row.name, row.isDir);
          await copyEntry(row.path, dst);
          last = dst;
        }
      } catch (e) {
        // Same partial-batch refresh as doPaste: run() refetches only on full
        // success, so copies already written would stay invisible here until
        // the dir-watch update. Rethrown so the error toast still shows.
        if (last !== null) refetch();
        throw e;
      }
      if (last !== null) pendingSelectRef.current = last; // select the new copy
    }, { verb: "duplicate", name: batchLabel(rows) }).finally(() => {
      duplicateInFlight.current = false;
    });
  };

  // Compress a folder into a sibling archive. In-flight guard for the same
  // reason Duplicate has one: two quick picks would race freeArchivePath to the
  // same free name and 409 the second. The new archive is selected on success,
  // matching what Duplicate does with its copy.
  const compressInFlight = useRef(false);
  const doCompress = (row: RowCtx, format: ArchiveFormat, ext: string) => {
    if (compressInFlight.current) return;
    compressInFlight.current = true;
    run(async () => {
      const dst = await freeArchivePath(row.parentDir, row.name, ext);
      await compressEntry(row.path, format, dst);
      pendingSelectRef.current = dst;
    }, { verb: "compress", name: row.name }).finally(() => {
      compressInFlight.current = false;
    });
  };

  // Lazy loader for the Compress submenu. The git-repo probe is a subprocess on
  // the server, so it runs here — once, on hover — rather than on every
  // right-click; a failed probe just drops the git entries (fail closed, like
  // the Open With condition gate).
  const loadCompress = (row: RowCtx) => async (): Promise<MenuEntry[]> => {
    let isRepoRoot = false;
    try {
      isRepoRoot = (await gitRepoInfo(row.path)).is_repo_root;
    } catch {
      isRepoRoot = false;
    }
    return buildCompressItems(isRepoRoot, (format, ext) => doCompress(row, format, ext));
  };

  const doReveal = (path: string) => {
    revealPath(path).catch((e) =>
      pushToast({ msg: friendlyFsError(e, { verb: "reveal", name: basename(path) }), tone: "error" })
    );
  };

  const doCopyPath = (path: string) => {
    // Confirm with a non-error "info" toast; a failure (clipboard unavailable
    // or permission denied) stays silent — the path is still reachable via
    // Reveal in Finder.
    copyToClipboard(path).then((ok) => {
      if (ok) pushToast({ msg: "Path copied", tone: "info" });
    });
  };

  // Several paths go to the system clipboard newline-separated (what every file
  // manager writes for a multi-selection paste into a terminal or editor).
  const doCopyPaths = (paths: string[]) => {
    copyToClipboard(paths.join("\n")).then((ok) => {
      if (ok) pushToast({ msg: `${paths.length} paths copied`, tone: "info" });
    });
  };

  // Open Claude Code via its claude-cli:// scheme handler — a dir cwd's into
  // itself, a file cwd's into its parent and pre-fills an @-mention prompt.
  // Setting location.href to a custom scheme does not navigate the SPA away.
  const doOpenInClaude = (path: string, isDir: boolean, name: string, parentDir: string) => {
    window.location.href = claudeDeepLink(path, isDir, name, parentDir);
  };

  const startNewFile = (dir: string) =>
    setDialog({
      kind: "prompt",
      title: "New File",
      initial: "untitled.txt",
      confirmLabel: "Create",
      onConfirm: (name) => {
        if (rejectName(name)) return;
        // create=true: refuse (409 "conflict", surfaced as an error toast) if a
        // file with this name already exists, so New File never clobbers it.
        run(() => writeFile(join(normDir(dir), name), "", true), { verb: "create", name });
      },
    });

  const startNewFolder = (dir: string) =>
    setDialog({
      kind: "prompt",
      title: "New Folder",
      initial: "untitled folder",
      confirmLabel: "Create",
      onConfirm: (name) => {
        if (rejectName(name)) return;
        run(() => mkdir(join(normDir(dir), name)), { verb: "create", name });
      },
    });

  const startRename = (row: RowCtx) =>
    setDialog({
      kind: "prompt",
      title: "Rename",
      initial: row.name,
      confirmLabel: "Rename",
      selectStem: true,
      onConfirm: (name) => {
        if (name === row.name) return;
        if (rejectName(name)) return;
        const dst = join(normDir(row.parentDir), name);
        run(async () => {
          await renameEntry(row.path, dst);
          // Re-anchor onto the new name so the reloaded listing keeps this row
          // selected (and Enter opens the renamed file, not the dead old path).
          pendingSelectRef.current = dst;
          // The clipboard may still be pointing at the old path (or inside it,
          // if a renamed folder held the cut/copied entry) — repoint it so a
          // later Paste doesn't target a source that's now gone.
          remapClipboardPath(row.path, dst);
        }, { verb: "rename", name: row.name });
      },
    });

  // Hard delete, confirmed. Plural-aware: one row still names it (and says
  // whether it's a folder), several are counted.
  const startDelete = (allRows: RowCtx[]) => {
    // Drop rows contained by another selected folder before anything else, so
    // the confirm dialog counts what will actually be deleted and the loop below
    // never calls deleteEntry on a path the parent's recursive delete just took
    // (that 404 would abort the batch and toast a failure for a delete that in
    // fact removed everything asked for).
    const rows = pruneDescendantRows(allRows);
    if (!rows.length) return;
    const many = rows.length > 1;
    setDialog({
      kind: "confirm",
      title: many ? `Delete ${rows.length} items` : "Delete",
      message: many
        ? `Delete these ${rows.length} items? Any folders among them are deleted with everything inside. This can't be undone.`
        : rows[0].isDir
        ? `Delete the folder "${rows[0].name}" and everything inside it? This can't be undone.`
        : `Delete "${rows[0].name}"? This can't be undone.`,
      confirmLabel: many ? `Delete ${rows.length} items` : "Delete",
      danger: true,
      // recursive=true for a directory (its contents were named in the message).
      onConfirm: () =>
        run(async () => {
          let deleted = 0;
          try {
            for (const row of rows) {
              await deleteEntry(row.path, row.isDir);
              notePathDeleted(row.path);
              deleted++;
            }
          } catch (e) {
            // Partial batch: run() refetches only on full success, so without
            // this the already-deleted rows linger in the listing until the
            // dir-watch update. Rethrown so run() still toasts the failure.
            if (deleted) refetch();
            throw e;
          }
        }, { verb: "delete", name: batchLabel(rows) }),
    });
  };

  // Move to Bin: a recoverable delete (macOS Trash), so no confirm dialog.
  // Acts on every row passed in (the whole selection). Where the server can't
  // trash (non-macOS → "unsupported") those rows fall back to the existing
  // confirm-then-hard-delete flow, which IS irreversible and so keeps its
  // warning. Success shows a low-key, count-aware info toast.
  const doTrash = (allRows: RowCtx[]) => {
    // As in startDelete: trashing a folder takes everything inside it, so a
    // selection that also holds rows from within that folder must not trash them
    // individually — the second call would hit a vanished path and be counted as
    // a real failure, replacing the "Moved to Bin" toast with a bogus error.
    const rows = pruneDescendantRows(allRows);
    if (!rows.length) return;
    void (async () => {
      const trashed: RowCtx[] = [];
      const unsupported: RowCtx[] = [];
      let failed: { row: RowCtx; message: string } | null = null;
      for (const row of rows) {
        const r = await trashEntry(row.path, row.isDir);
        if (r.status === "trashed") {
          trashed.push(row);
          notePathDeleted(row.path);
        } else if (r.status === "unsupported") {
          unsupported.push(row);
        } else if (failed === null) {
          failed = { row, message: r.message };
        }
      }
      if (trashed.length) {
        pushToast({
          msg: trashed.length === 1 ? "Moved to Bin" : `Moved ${trashed.length} items to Bin`,
          tone: "info",
        });
        refetch();
      }
      // A real failure raises its own toast. It used to REPLACE the info one
      // above (one local slot, last write wins), which hid the fact that the
      // other rows did move; the shared stack shows both, which is what a
      // partial success actually is. The unsupported fallback only runs when
      // nothing errored.
      if (failed !== null) {
        pushToast({
          msg: friendlyFsError(failed.message, { verb: "move to Bin", name: failed.row.name }),
          tone: "error",
        });
      } else if (unsupported.length) {
        startDelete(unsupported);
      }
    })();
  };

  // Lazy loader for the Open With submenu: resolves the entry's template modes
  // (resolveOpenWithModes mirrors Preview's filter + condition-gate handling).
  // Selecting a mode navigates to the entry with `_mode` set; the default mode
  // deletes the param.
  const loadOpenWith = (path: string) => async (): Promise<MenuItem[]> => {
    const modes = await resolveOpenWithModes(path);
    return buildOpenWithItems(modes, (mode, isDefault) => {
      const search = isDefault ? "" : "?_mode=" + encodeURIComponent(mode);
      navigateUrl(urlForFsPath(path, search));
    });
  };

  // Menu for a right-clicked row (file or dir), in macOS Finder order. Paste
  // target follows Finder: into a dir, or the parent of a file. New File/Folder
  // live only on the background menu (Finder shows them there, not on a row).
  // `rows` is what the menu ACTS on: just the right-clicked row normally, or the
  // whole selection when the right-click landed inside a multi-row selection
  // (see openRowMenu in Listing.tsx). With several rows the entries that only
  // make sense for one — Open / Open With / Rename / Reveal / Open in Claude
  // Code — are dropped, and the batch entries count what they'll affect.
  const rowMenu = (row: RowCtx, rows: RowCtx[]): MenuEntry[] => {
    const dir = targetDirOf(row);
    const n = rows.length;
    if (n > 1) {
      return [
        { label: `Move ${n} items to Bin`, icon: MenuIcons.trash, onClick: () => doTrash(rows) },
        "separator",
        { label: `Duplicate ${n} items`, icon: MenuIcons.duplicate, onClick: () => doDuplicate(rows) },
        "separator",
        {
          label: `Cut ${n} items`,
          icon: MenuIcons.cut,
          onClick: () => setClipboard({ paths: rows.map((r) => r.path), op: "cut" }),
        },
        {
          label: `Copy ${n} items`,
          icon: MenuIcons.copy,
          onClick: () => setClipboard({ paths: rows.map((r) => r.path), op: "copy" }),
        },
        { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
        "separator",
        {
          label: `Copy ${n} Paths`,
          icon: MenuIcons.copyPath,
          onClick: () => doCopyPaths(rows.map((r) => r.path)),
        },
      ];
    }
    return [
      { label: isAppEntry(row.name, row.isDir) ? "Open App" : "Open", icon: MenuIcons.open, onClick: () => navigate(row.path, { isDir: row.isDir }) },
      { label: "Open With", icon: MenuIcons.openWith, submenu: loadOpenWith(row.path) },
      "separator",
      { label: "Move to Bin", icon: MenuIcons.trash, onClick: () => doTrash([row]) },
      "separator",
      { label: "Rename…", icon: MenuIcons.rename, onClick: () => startRename(row) },
      { label: "Duplicate", icon: MenuIcons.duplicate, onClick: () => doDuplicate([row]) },
      // Folders only, in Finder's position (after Duplicate, before Cut/Copy).
      // Not on the multi-select or background menus: one archive per folder.
      ...(row.isDir
        ? [{ label: "Compress", icon: MenuIcons.compress, submenu: loadCompress(row) } as MenuEntry]
        : []),
      "separator",
      { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ paths: [row.path], op: "cut" }) },
      { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ paths: [row.path], op: "copy" }) },
      { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
      "separator",
      { label: "Copy Path", icon: MenuIcons.copyPath, onClick: () => doCopyPath(row.path) },
      { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(row.path) },
      {
        label: "Open in Claude Code",
        icon: MenuIcons.openWith,
        onClick: () => doOpenInClaude(row.path, row.isDir, row.name, row.parentDir),
      },
    ];
  };

  // Menu for the empty listing background — operates on the current folder.
  // Finder order: New Folder before New File.
  const backgroundMenu = (): MenuEntry[] => [
    { label: "New Folder…", icon: MenuIcons.newFolder, onClick: () => startNewFolder(base) },
    { label: "New File…", icon: MenuIcons.newFile, onClick: () => startNewFile(base) },
    "separator",
    { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(base) },
    "separator",
    { label: "Refresh", icon: MenuIcons.refresh, onClick: refetch },
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(normDir(base)) },
    {
      label: "Open in Claude Code",
      icon: MenuIcons.openWith,
      onClick: () => doOpenInClaude(normDir(base), true, "", normDir(base)),
    },
  ];

  return {
    menu,
    setMenu,
    dialog,
    setDialog,
    doPaste,
    doDuplicate,
    doTrash,
    startRename,
    startNewFolder,
    rowMenu,
    backgroundMenu,
  };
}
