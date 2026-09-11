// File operations on listing rows (paste, move, undo/redo, duplicate, compress,
// trash, delete, rename, new file/folder, reveal, copy-path, open-in-Claude)
// plus the context menus built from them. Owns the dialog + context-menu state;
// toasts go to the global store (lib/toast), and the cut/copy clipboard and the
// undo stack are module-level stores (lib/fs-clipboard, lib/fs-undo) — all three
// survive this component's per-folder remount, which a move can cause mid-flight
// by spring-loading a crumb.
import { useEffect, useRef, useState } from "react";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import {
  writeFile,
  mkdir,
  deleteEntry,
  renameEntry,
  copyEntry,
  compressEntry,
  downloadAppFile,
  gitRepoInfo,
  statPath,
  revealPath,
  getConfig,
} from "@platform/lib/api";
import type { ArchiveFormat } from "@platform/lib/api";
import {
  normDir,
  join,
  dirname,
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
  claudeTerminalCommand,
} from "@apps/explorer/lib/fs-actions";
import { moveEntriesInto } from "@apps/explorer/lib/fs-move";
import { crumbMenu, folderBarMenu, withFolderRename, type RenameBaseGuard } from "@apps/explorer/lib/bar-menus";
import { enterPanel } from "@apps/explorer/lib/split-actions";
import { publishTopbarMenu } from "@apps/explorer/topbar-menu";
import {
  applyFsOp,
  beginFsUndo,
  endFsUndo,
  fsUndoEpoch,
  invertFsOp,
  pushRedoOp,
  pushUndoOp,
  recordFsOp,
  relocationToast,
  takeRedoOp,
  takeUndoOp,
  trashUndoPairs,
  type FsOp,
} from "@apps/explorer/lib/fs-undo";
import { basename } from "@platform/lib/format";
import { getClipboard, setClipboard, type Clipboard } from "@apps/explorer/lib/fs-clipboard";
import { dismissPopup, notify } from "@platform/lib/notifications";
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
  ownsBar,
}: {
  base: string;
  clipboard: Clipboard | null;
  refetch: () => void;
  // A path the selection should jump to once it appears in the reloaded rows
  // (a rename/duplicate target — its row doesn't exist until the refetch lands).
  pendingSelectRef: React.MutableRefObject<string | null>;
  // "This folder view owns the window's crumb bar" (Listing's ownsBarChrome —
  // the same claim that moves the bar into this column). While it holds, a
  // right-click ANYWHERE on that bar opens this folder's menu, published through
  // topbar-menu.ts. A listing embedded in a preview pane has its own chrome and
  // passes false, or the bar would answer with the wrong folder's actions.
  ownsBar?: boolean;
}) {
  // The open context menu (position + items) and the open modal, both local to
  // this folder view.
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);

  // Home + mounts-root, for backgroundMenu's "may THIS folder be renamed?"
  // guard (bar-menus.canRenameBase). Fetched once and starts empty, which the
  // guard reads as "fail closed" — no Rename item flashes on before /api/config
  // answers, well before a user's first right-click in practice.
  const [renameGuard, setRenameGuard] = useState<RenameBaseGuard>({});
  // Read once at mount and, should that read fail, again on the next menu open
  // (bugbot, PR #1049): a failed read used to leave the guard empty for the
  // session, and the guard now fails CLOSED on empty, so without a re-read one
  // blip at mount would hide Rename for the page's life. Re-read on demand
  // rather than on a timer — nothing here should keep a clock running.
  const guardLoadedRef = useRef(false);
  const guardInFlightRef = useRef(false);
  const loadRenameGuard = () => {
    if (guardLoadedRef.current || guardInFlightRef.current) return;
    guardInFlightRef.current = true;
    getConfig().then((c) => {
      guardLoadedRef.current = true;
      setRenameGuard({
        home: String(c.home ?? "").replace(/\\/g, "/"),
        mountsRoot: String(c.mounts_root ?? "").replace(/\\/g, "/"),
      });
    }, () => {}).finally(() => {
      guardInFlightRef.current = false;
    });
  };
  useEffect(loadRenameGuard, []);

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
      notify({ title: ctx ? friendlyFsError(e, ctx) : (e as Error).message, tone: "error" });
    }
  };

  // Belt-and-braces name guard for the New File / New Folder / Rename handlers:
  // the dialog already blocks invalid names, but re-check here (and toast) before
  // building a path so a "." / ".." / separator can never escape the folder.
  // Returns true when the name is rejected (caller should bail).
  const rejectName = (name: string): boolean => {
    const err = nameError(name);
    if (err) notify({ title: err, tone: "error" });
    return err !== null;
  };

  // Guards a paste that's still running so a second Paste gesture (a rapid
  // Cmd+V×2) can't fire a parallel op on the same source — for a cut that
  // second call would renameEntry an already-moved src and 404 with a jarring
  // toast. Reset in the flight's .finally, so sequential copy-pastes stay fine.
  const pasteInFlight = useRef(false);
  // Set by the progress toast's Cancel action, read at the top of every loop
  // iteration below. A copy already issued is not undone — the loop simply
  // stops asking for the next one, leaving whatever landed in place.
  const pasteCancelRef = useRef(false);

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
    // Progress + cancel is a COPY thing (decision 12): a cut is a rename per
    // entry, near-instant, and its clipboard is already gone. A single-file
    // copy is also skipped — there is nothing to report progress ABOUT.
    const showProgress = op === "copy" && paths.length > 1;
    pasteCancelRef.current = false;
    let progressToastId: number | undefined;
    const progressAction = {
      label: "Cancel",
      onClick: () => {
        pasteCancelRef.current = true;
      },
    };
    if (showProgress) {
      progressToastId = notify({
        title: `Copying 1 of ${paths.length}…`,
        tone: "info",
        action: progressAction,
      });
    }
    run(async () => {
      const pasted: string[] = [];
      // A CUT paste is a relocation, so it goes on the undo stack — with the
      // destinations that actually landed, deduped names and all (lib/fs-undo).
      // A COPY paste does not: its inverse is a delete, which is not something
      // an undo may do on the user's behalf.
      const relocated: { from: string; to: string }[] = [];
      let last: string | null = null;
      let cancelled = false;
      try {
        for (let i = 0; i < paths.length; i++) {
          const src = paths[i];
          if (showProgress && pasteCancelRef.current) {
            cancelled = true;
            break;
          }
          if (showProgress) {
            // Reassign: once the popup has already auto-expired (a prior file
            // ran longer than JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS), notify()
            // mints a FRESH id rather than replacing a card that's gone — if
            // progressToastId stayed frozen at the original (dead) id, every
            // remaining iteration would repeat that same miss and pop a new
            // card each time instead of updating the one now on screen.
            progressToastId = notify(
              { title: `Copying ${i + 1} of ${paths.length}…`, tone: "info", action: progressAction },
              progressToastId,
            );
          }
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
          if (op === "cut") {
            await renameEntry(src, dst);
            relocated.push({ from: src, to: dst });
          } else await copyEntry(src, dst);
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
        // A half-moved cut is still a relocation of everything that DID move,
        // and it is the case a user most wants back — recorded before the
        // rethrow, since run()'s error path never reaches the lines below.
        if (relocated.length) recordFsOp({ kind: "move", pairs: relocated });
        if (progressToastId !== undefined) dismissPopup();
        throw e;
      }
      if (relocated.length) recordFsOp({ kind: "move", pairs: relocated });
      // Re-anchor onto the last thing written, if it lands in this view.
      if (last !== null) pendingSelectRef.current = last;
      if (progressToastId !== undefined) {
        dismissPopup();
        // Already-copied files stay exactly where they landed — cancelling
        // stops the loop from asking for the next one, nothing more.
        if (cancelled) {
          notify({ title: `Copy cancelled — ${pasted.length} of ${paths.length} copied`, tone: "info" });
        }
      }
    }, { verb: "paste", name: label }).finally(() => {
      pasteInFlight.current = false;
    });
  };

  // Drop-to-move: put `paths` into `targetDir`. The move itself is the shared
  // one (lib/fs-move) — the same conflict resolution, descendant pruning and
  // clipboard repointing a cut-and-paste gets, because a drag onto a folder IS
  // a cut and paste with the target picked by the pointer. What is local to
  // this view is only the aftermath: refresh the listing, and re-anchor onto
  // the last thing written so the moved entries stay selected WHERE THEY ARE
  // STILL VISIBLE (a search listing that spans the target folder, mostly —
  // moving out of the folder you are looking at takes the rows off screen, and
  // the reconcile's clamp then lands the selection on a surviving neighbour).
  //
  // In-flight guard for the same reason paste has one: a second drop landing
  // mid-batch would rename sources the first is already moving.
  const moveInFlight = useRef(false);
  // `announce` comes from the DROP TARGET, not from this view: a drop onto a
  // sidebar bookmark lands somewhere the user cannot see, so that move says so
  // (moveEntriesInto's toast). A drop onto a row or the listing background
  // refreshes under the cursor, which is its own confirmation.
  const doMove = (paths: string[], targetDir: string, opts?: { announce?: boolean }) => {
    if (!paths.length || moveInFlight.current) return;
    moveInFlight.current = true;
    void (async () => {
      try {
        // Per-entry failures are reported by moveEntriesInto itself (one toast
        // wherever the drop landed) and come back in the report; what is caught
        // here is the batch failing OUTSIDE that loop — planning it, or the
        // reporting itself. Rare, and precisely why it must not be swallowed:
        // an unexplained no-op is the worst outcome a drop can have.
        const report = await moveEntriesInto(paths, targetDir, {
          announce: opts?.announce ?? false,
        });
        if (!report.moved.length) return;
        // Undoable, from the pairs that ACTUALLY landed — the destination may be
        // a deduped "… copy" name, and a partial batch has fewer pairs than it
        // was asked for (see MoveReport.pairs).
        recordFsOp({ kind: "move", pairs: report.pairs });
        pendingSelectRef.current = report.moved[report.moved.length - 1];
        refetch();
      } catch (e) {
        notify({
          title: friendlyFsError(e, {
            verb: "move",
            name: paths.length === 1 ? basename(paths[0]) : `${paths.length} items`,
          }),
          tone: "error",
        });
      } finally {
        // FINALLY, not after the await: a rejection used to leave this latched
        // at true, and with it latched every later drag-to-move in this listing
        // returned at the guard above — the feature silently dead for the life
        // of the view, with nothing said. Same shape as paste/duplicate's own
        // .finally, which is what this had drifted from.
        moveInFlight.current = false;
      }
    })();
  };

  // Undo / redo of the explorer's RELOCATIONS — a drag-move, a cut-paste and a
  // rename, the three ops that are a rename in both directions (lib/fs-undo
  // explains why nothing else is on the stack).
  //
  // ONE implementation for both directions, because they are the same operation
  // with the stacks swapped: take the top entry, apply its inverse, and put what
  // landed onto the OTHER stack (inverting twice is the original op). Writing
  // them separately would be two chances to get the "what goes back on which
  // stack" half wrong.
  //
  // The in-flight guard is NOT a ref here — it lives in lib/fs-undo with the
  // stacks (beginFsUndo). A ref in this hook died with the component, and a move
  // can navigate mid-gesture (spring-loading a crumb), so it was reset to false
  // exactly when a second Cmd+Z must not be allowed through. Shared by both
  // directions deliberately: an undo racing a redo is two renames of the same
  // paths in opposite directions.
  const runRelocation = (
    take: () => FsOp | null,
    pushOther: (op: FsOp, atEpoch: number) => void,
    pushSame: (op: FsOp, atEpoch: number) => void,
    verb: "undo" | "redo",
  ) => {
    if (!beginFsUndo()) return;
    const op = take();
    if (!op) {
      endFsUndo();
      return;
    }
    // Read BEFORE the renames start. A relocation recorded while they run revokes
    // this gesture's redo entry, and comparing epochs at the push is what makes
    // that hold across the await (lib/fs-undo's pushRedoOp).
    const startedAt = fsUndoEpoch();
    void applyFsOp(invertFsOp(op))
      .then((report) => {
        if (report.done.length) {
          // What landed is what the other direction can put back. The per-path
          // FAILURES go on neither stack: the entry was taken off before the
          // attempt, and one that 404s or 409s would otherwise sit at the top
          // failing for every later Undo.
          pushOther({ kind: op.kind, pairs: report.done }, startedAt);
          // THE FOLDER ON SCREEN MAY BE WHAT MOVED (bugbot, PR #1049): undoing
          // a rename of the current folder puts it back under its old name,
          // and a refetch of `base` would read a path that no longer exists.
          // Follow it instead — the same navigation the rename itself made —
          // and refetch only when the rows moved but the room did not.
          const here = normDir(base);
          const carried = report.done.find(
            (pair) => here === pair.from || here.startsWith(pair.from + "/"),
          );
          if (carried) {
            navigateUrl(urlForFsPath(carried.to + here.slice(carried.from.length), location.search));
          } else {
            pendingSelectRef.current = report.done[report.done.length - 1].to;
            refetch();
          }
        }
        // Everything a SYSTEMIC refusal left undone — the pair it refused and the
        // pairs it never reached — goes back on the stack this gesture took the op
        // from, so it all stays undoable once the cause is fixed. The failure is
        // environmental rather than a verdict about those paths, and abandoning
        // any of them would be the hazard the every-pair change removed.
        // Re-inverted, because the stack holds ops in the original direction while
        // `pending` is in the inverse one.
        if (report.pending.length) {
          pushSame(invertFsOp({ kind: op.kind, pairs: report.pending }), startedAt);
        }
        // ONE toast, always, and it tells the whole outcome — built in lib/fs-undo
        // so its arithmetic (which path is blamed, what the retry count covers) is
        // testable without a renderer.
        // relocationToast's "Undid/Redid the <kind>" success message is a
        // completed move/delete/copy record - the same "destructive-but-
        // successful" case fs-move.ts's own move confirmation is (spec's
        // motivating example); its failure branch stays attention (the
        // default for tone: "error").
        {
          const t = relocationToast(verb, op.kind, report);
          notify({ title: t.msg, tone: t.tone, tier: t.tone === "info" ? "trail" : undefined });
        }
      })
      .finally(() => {
        // FINALLY, for the reason spelled out on moveInFlight above: a latched
        // guard would silently kill undo for the life of the SESSION now that it
        // lives in the module rather than in this component.
        endFsUndo();
      });
  };
  const doUndo = () => runRelocation(takeUndoOp, pushRedoOp, pushUndoOp, "undo");
  const doRedo = () => runRelocation(takeRedoOp, pushUndoOp, pushRedoOp, "redo");

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
      notify({ title: friendlyFsError(e, { verb: "reveal", name: basename(path) }), tone: "error" })
    );
  };

  const doCopyPath = (path: string) => {
    // Confirm with a non-error "info" toast; a failure (clipboard unavailable
    // or permission denied) stays silent — the path is still reachable via
    // Reveal in Finder.
    copyToClipboard(path).then((ok) => {
      if (ok) notify({ title: "Path copied", tone: "info" });
    });
  };

  // Several paths go to the system clipboard newline-separated (what every file
  // manager writes for a multi-selection paste into a terminal or editor).
  const doCopyPaths = (paths: string[]) => {
    copyToClipboard(paths.join("\n")).then((ok) => {
      if (ok) notify({ title: `${paths.length} paths copied`, tone: "info" });
    });
  };

  // Hand the user the command instead of launching anything: a dir cd's into
  // itself, a file into its parent, and the paste happens in the terminal (and
  // the session) they already chose. Same shape as the config app's install
  // commands — copy, then say so.
  const doOpenInClaude = (path: string, isDir: boolean, parentDir: string) => {
    copyToClipboard(claudeTerminalCommand(path, isDir, parentDir)).then((ok) => {
      if (ok) notify({ title: "Command copied — paste it in your terminal", tone: "info" });
    });
  };

  // Open in New Tab — the SAME url the row's ordinary click navigates to
  // (urlForFsPath, which is what `navigate` pushes), handed to the browser
  // instead of to history. No search string: `navigate` starts a destination on
  // a fresh query string too, so the new tab lands where a click would.
  // `noopener` because the opened tab has no business reaching back at us.
  const doOpenInNewTab = (path: string) => {
    window.open(urlForFsPath(path), "_blank", "noopener");
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
          // Undoable, and the commoner reflex than undoing a drag: Cmd+Z after a
          // misnamed file. Recorded AFTER the rename resolved — an op that never
          // happened must not be on the stack (lib/fs-undo).
          recordFsOp({ kind: "rename", pairs: [{ from: row.path, to: dst }] });
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

  // Rename the CURRENT folder itself — the crumb bar / folder background
  // menu's "Rename…" (gated by canRenameBase, see backgroundMenu below), not
  // a row inside it. No `selectStem`: a folder name has no extension to
  // spare, so the whole name is selected, unlike startRename's file case.
  // Navigates to the new path on success (rather than pendingSelectRef, which
  // only re-anchors a ROW in a listing that stays mounted) so the crumb bar —
  // and everything else keyed on this URL — picks up the new name.
  const startRenameFolder = (dir: string) =>
    setDialog({
      kind: "prompt",
      title: "Rename",
      initial: basename(dir),
      confirmLabel: "Rename",
      onConfirm: (name) => {
        if (name === basename(dir)) return;
        if (rejectName(name)) return;
        const dst = join(dirname(dir), name);
        // Not through `run`: its refetch would re-read the OLD path after the
        // navigation below has already left it — a fetch of a folder that no
        // longer exists, landing on a view that is being unmounted.
        void renameEntry(dir, dst).then(
          () => {
            recordFsOp({ kind: "rename", pairs: [{ from: dir, to: dst }] });
            remapClipboardPath(dir, dst);
            navigateUrl(urlForFsPath(dst, location.search));
          },
          (e: unknown) =>
            notify({ title: friendlyFsError(e, { verb: "rename", name: basename(dir) }), tone: "error" }),
        );
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

  // Delete: a recoverable delete (moves the rows to the OS bin), so no confirm
  // dialog. Acts on every row passed in (the whole selection). Where the server
  // can't trash a row ("unsupported" — a remote mount, a Linux cross-device move,
  // a platform with no backend) THOSE rows fall back to the existing
  // confirm-then-hard-delete flow, which IS irreversible and so keeps its
  // warning. Since every desktop platform now has a bin backend, that dialog has
  // stopped being the ordinary Windows/Linux delete and is what it always claimed
  // to be: the irreversible case. Success shows a low-key, count-aware info toast.
  //
  // UNDOABLE WHERE THE DESTINATION IS NAMED, as one op for the whole batch: on
  // macOS-local and Linux-XDG the trash is a rename the server chose the
  // destination for (`to`), so the delete records exactly the pair a move would
  // and Cmd+Z moves the entries back out (lib/fs-undo). The Recycle Bin and the
  // macOS Finder fallback name nothing, so those rows are recoverable through the
  // OS and not through Cmd+Z.
  const doTrash = (allRows: RowCtx[]) => {
    // As in startDelete: trashing a folder takes everything inside it, so a
    // selection that also holds rows from within that folder must not trash them
    // individually — the second call would hit a vanished path and be counted as
    // a real failure, replacing the "Deleted" toast with a bogus error.
    const rows = pruneDescendantRows(allRows);
    if (!rows.length) return;
    void (async () => {
      // Where each trashed row WENT, for the undo op. `to` is absent wherever the
      // OS owns the location — the Windows Recycle Bin, and the macOS
      // cross-device Finder fallback. Those rows ARE in the bin and recoverable
      // from the OS's own UI, but they contribute NO pair (trashUndoPairs drops
      // them), because an undo needs a path to move back FROM and inventing one
      // would aim it at nothing.
      const trashed: { path: string; to?: string }[] = [];
      const unsupported: RowCtx[] = [];
      let failed: { row: RowCtx; message: string } | null = null;
      for (const row of rows) {
        const r = await trashEntry(row.path, row.isDir);
        if (r.status === "trashed") {
          trashed.push({ path: row.path, to: r.to });
          // ACCEPTED LOSS: this drops the entry from Recents, and an undo does
          // not put it back — the restore is a rename, and nothing re-notes the
          // path as opened. The entry returns to the filesystem where it was;
          // only its place in the recents list is gone.
          notePathDeleted(row.path);
        } else if (r.status === "unsupported") {
          unsupported.push(row);
        } else if (failed === null) {
          failed = { row, message: r.message };
        }
      }
      if (trashed.length) {
        // Destructive-but-successful (spec's own motivating example): the user
        // should be able to find out what was deleted after the card is gone.
        notify({
          title: trashed.length === 1 ? "Deleted" : `Deleted ${trashed.length} items`,
          tone: "info",
          tier: "trail",
        });
        // One op for the batch, so a single Cmd+Z brings the whole selection
        // back. Guarded on emptiness EXPLICITLY even though recordFsOp's push
        // already no-ops on it: a batch that was entirely Finder-trashed yields
        // no pairs, and "there is nothing here to undo" is the intent rather
        // than a coincidence of what push happens to do.
        const pairs = trashUndoPairs(trashed);
        if (pairs.length) recordFsOp({ kind: "delete", pairs });
        refetch();
      }
      // A real failure raises its own toast. It used to REPLACE the info one
      // above (one local slot, last write wins), which hid the fact that the
      // other rows did move; the shared stack shows both, which is what a
      // partial success actually is. The unsupported fallback only runs when
      // nothing errored.
      if (failed !== null) {
        notify({
          title: friendlyFsError(failed.message, { verb: "delete", name: failed.row.name }),
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
  // make sense for one — Open / Open in New Tab / Open With / Rename / Reveal /
  // Copy Claude session command — are dropped, and the batch entries count what
  // they'll affect. A single row — file OR folder — gets the full list (D404;
  // the two-item minimal menu of D398 survives only on ancestor crumbs, D399).
  const rowMenu = (row: RowCtx, rows: RowCtx[]): MenuEntry[] => {
    const dir = targetDirOf(row);
    const n = rows.length;
    if (n > 1) {
      return [
        { label: `Delete ${n} items`, icon: MenuIcons.trash, onClick: () => doTrash(rows) },
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
      { label: "Open", icon: MenuIcons.open, onClick: () => navigate(row.path, { isDir: row.isDir }) },
      { label: "Open in New Tab", icon: MenuIcons.newTab, onClick: () => doOpenInNewTab(row.path) },
      { label: "Open With", icon: MenuIcons.openWith, submenu: loadOpenWith(row.path) },
      "separator",
      { label: "Delete", icon: MenuIcons.trash, onClick: () => doTrash([row]) },
      "separator",
      { label: "Rename…", icon: MenuIcons.rename, onClick: () => startRename(row) },
      { label: "Duplicate", icon: MenuIcons.duplicate, onClick: () => doDuplicate([row]) },
      // Folders only, in Finder's position (after Duplicate, before Cut/Copy).
      // Not on the multi-select or background menus: one archive per folder.
      ...(row.isDir
        ? [{ label: "Compress", icon: MenuIcons.compress, submenu: loadCompress(row) } as MenuEntry]
        : []),
      // Folders only, like Compress: the whole folder as one .fused app file
      // (SPEC §43 AF-4). Offered on every folder rather than probing the app
      // entry up front — the export route validates server-side and its
      // "not a fused app" reason surfaces as the toast.
      ...(row.isDir
        ? [{
            label: "Export App File",
            icon: MenuIcons.compress,
            onClick: () => {
              downloadAppFile(row.path, row.name).catch((e: Error) =>
                notify({ title: "Could not export " + row.name + ": " + e.message, tone: "error" }),
              );
            },
          } as MenuEntry]
        : []),
      "separator",
      { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ paths: [row.path], op: "cut" }) },
      { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ paths: [row.path], op: "copy" }) },
      { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
      "separator",
      { label: "Copy Path", icon: MenuIcons.copyPath, onClick: () => doCopyPath(row.path) },
      { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(row.path) },
      {
        label: "Copy Claude session command",
        icon: MenuIcons.openWith,
        onClick: () => doOpenInClaude(row.path, row.isDir, row.parentDir),
      },
    ];
  };

  // Menu for the empty listing background — operates on the current folder.
  // Finder order: New Folder before New File. "Rename…" leads the list
  // (mirroring fileBarMenu's own item-then-separator opener) whenever
  // canRenameBase allows renaming THIS folder — root, home and mount roots
  // never get it (withFolderRename, bar-menus.ts).
  const backgroundMenu = (): MenuEntry[] => {
    loadRenameGuard(); // a failed mount-time read gets another go on every open
    return withFolderRename(
      [
        { label: "New Folder…", icon: MenuIcons.newFolder, onClick: () => startNewFolder(base) },
        { label: "New File…", icon: MenuIcons.newFile, onClick: () => startNewFile(base) },
        "separator",
        { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(base) },
        "separator",
        { label: "Refresh", icon: MenuIcons.refresh, onClick: refetch },
        { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(normDir(base)) },
        // Beside Reveal: both are "this folder, but elsewhere". Here the folder is
        // the one being listed, so the new tab opens on the current directory.
        {
          label: "Open in New Tab",
          icon: MenuIcons.newTab,
          onClick: () => doOpenInNewTab(normDir(base)),
        },
        { label: "Copy path", icon: MenuIcons.copyPath, onClick: () => doCopyPath(normDir(base)) },
        {
          label: "Copy Claude session command",
          icon: MenuIcons.openWith,
          onClick: () => doOpenInClaude(normDir(base), true, normDir(base)),
        },
      ],
      normDir(base),
      renameGuard,
      () => startRenameFolder(normDir(base))
    );
  };

  // The folder's menu as the CRUMB BAR offers it: this folder's own actions plus
  // the splits — item for item what the middle panel's header `⋮` shows, because
  // it is the same builder (lib/bar-menus). Two surfaces, one list.
  const barMenu = (): MenuEntry[] => folderBarMenu(backgroundMenu(), (dir) => enterPanel(base, dir));

  // Hand that menu to the crumb bar for as long as this view owns it.
  //
  // Through a ref, not by re-publishing: `barMenu` closes over the clipboard
  // (Paste's disabled state), `base` and the dialog setters, so a captured
  // function would go stale within a keystroke — and re-running the effect on
  // every change would churn the publish/release pair for no reason. The
  // published thunk is stable and reads the current one.
  //
  // `crumb` is an ANCESTOR crumb the right-click landed on (Breadcrumb's
  // onBarContextMenu): a folder that is not `base`, so the folder menu above —
  // New File, Paste, Refresh, all about `base` — is the wrong list for it. It
  // gets the ancestor pair instead.
  const openBarMenuRef = useRef<(x: number, y: number, crumb?: string) => void>(() => {});
  openBarMenuRef.current = (x, y, crumb) =>
    setMenu({
      x,
      y,
      items: crumb
        ? crumbMenu({
            onReveal: () => doReveal(crumb),
            onOpenInNewTab: () => doOpenInNewTab(crumb),
          })
        : barMenu(),
    });
  useEffect(() => {
    if (!ownsBar) return;
    return publishTopbarMenu((x, y, crumb) => openBarMenuRef.current(x, y, crumb));
  }, [ownsBar]);

  return {
    menu,
    setMenu,
    barMenu,
    dialog,
    setDialog,
    doPaste,
    doMove,
    doUndo,
    doRedo,
    doDuplicate,
    doTrash,
    startRename,
    startRenameFolder,
    startNewFolder,
    rowMenu,
    backgroundMenu,
  };
}
