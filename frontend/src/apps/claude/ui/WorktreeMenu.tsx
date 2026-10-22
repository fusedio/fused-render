// THE WORKTREE MENU (SPEC W-4) — in the top bar of a chat on a FILE OR FOLDER
// inside a git repository: which checkout this chat is working in, and the door
// to the others.
//
// SWITCHING IS NAVIGATING. A worktree is just another folder, so moving to one
// is opening the chat's same file (or the worktree's folder, when the file is
// not on that branch — `switchTarget`) with no session — `onSwitch(path)`,
// which the host turns into the same URL the Home lists open a chat on
// (`list-rows.paneChatUrl`). Nothing here knows a URL.
//
// The popover is the pills' own (`PillSelect`: shadcn's Popover over Base UI)
// rather than the ⋮'s dropdown, because one of its states is a text field and a
// menu's typeahead would eat the keystrokes.
import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { confirmLeave } from "@platform/lib/router";
import {
  defaultWorktreeName,
  dirtyState,
  isDirtyRefusal,
  isWorktreeError,
  removeLabel,
  listWorktrees,
  createAndTarget,
  switchTarget,
  removeWorktree,
  resolveWorktree,
  triggerLabel,
  validWorktreeName,
  worktreeRows,
  type DirtyState,
  type WorktreeList,
  type WorktreeMenuRow,
} from "../protocol/worktrees";
import { useDismissOnWindow } from "./useDismissOnWindow";

/** A `/worktree [name]` from the composer: `seq` makes a repeat a new request. */
export interface WorktreeRequest {
  seq: number;
  name?: string;
}

export interface WorktreeMenuBodyProps {
  rows: readonly WorktreeMenuRow[];
  onSwitch(path: string): void;
  /** The inline "New worktree" field: null = closed, else its current value. */
  creating: string | null;
  onCreating(value: string | null): void;
  onCreate(name: string): void;
  /** The remove confirm's state for the current worktree. */
  confirming: boolean;
  /** The current worktree's dirt as the confirm reads it — the row's own,
   *  upgraded to "dirty" by a server refusal. Defaults to the row's. */
  dirt?: DirtyState;
  onRemove(): void;
  /** A turn is live: nothing may be removed from under it. */
  running?: boolean;
  busy?: boolean;
  error?: string;
}

/**
 * THE MENU'S CONTENTS, without the popover around them — so the rows, the
 * field and the confirm can be rendered and pressed in a test.
 */
export function WorktreeMenuBody({
  rows,
  onSwitch,
  creating,
  onCreating,
  onCreate,
  confirming,
  dirt,
  onRemove,
  running,
  busy,
  error,
}: WorktreeMenuBodyProps) {
  const current = rows.find((r) => r.current) ?? null;
  const fieldOk = creating !== null && validWorktreeName(creating.trim());
  return (
    <div className="chat-worktree-list" role="group" aria-label="Worktrees">
      {rows.map((row) => (
        <button
          key={row.path}
          type="button"
          className={"chat-worktree-row" + (row.current ? " is-current" : "")}
          aria-current={row.current ? "true" : undefined}
          title={row.path}
          disabled={busy}
          onClick={row.current ? undefined : () => onSwitch(row.target)}
        >
          <span className="chat-worktree-name">{row.name}</span>
          <span className="chat-worktree-branch">
            {row.branch}
            {row.missing && !row.current ? (
              <span className="chat-worktree-miss">opens the folder: file not on this branch</span>
            ) : null}
          </span>
          {row.dirty ? (
            <span className="chat-worktree-dirty" title="Uncommitted changes" aria-label="Uncommitted changes" />
          ) : null}
        </button>
      ))}
      <div className="chat-worktree-sep" />
      {creating === null ? (
        <button
          type="button"
          className="chat-worktree-row"
          disabled={busy}
          onClick={() => onCreating(defaultWorktreeName())}
        >
          New worktree…
        </button>
      ) : (
        <div className="chat-worktree-new">
          <input
            className="chat-worktree-field"
            aria-label="New worktree name"
            value={creating}
            autoFocus
            spellCheck={false}
            disabled={busy}
            onChange={(ev) => onCreating(ev.currentTarget.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Enter") {
                ev.preventDefault();
                if (fieldOk) onCreate(creating.trim());
              } else if (ev.key === "Escape") {
                // Esc cancels the FIELD, and only the field: the menu stays.
                ev.preventDefault();
                ev.stopPropagation();
                onCreating(null);
              }
            }}
          />
          {!fieldOk && creating.trim() ? (
            <div className="chat-worktree-note is-error">
              Letters, digits, dots, dashes or underscores, up to 64.
            </div>
          ) : null}
        </div>
      )}
      {current?.removable ? (
        <button
          type="button"
          className="chat-worktree-row is-danger"
          disabled={busy || running}
          onClick={running ? undefined : onRemove}
        >
          {removeLabel(confirming, dirt ?? dirtyState(current))}
        </button>
      ) : null}
      {current?.removable && running ? (
        <div className="chat-worktree-note">Stop the run before removing this worktree.</div>
      ) : null}
      {error ? <div className="chat-worktree-note is-error">{error}</div> : null}
    </div>
  );
}

/**
 * THE CONFIRMED REMOVE, in order: the leave guard FIRST — removing the folder
 * this chat is open on and then being told "stay" would strand the chat in a
 * folder that is gone — then the remove, forced unless the worktree is known
 * clean (the confirm the reader just pressed said so).
 */
export async function removeFlow(
  dirt: DirtyState,
  deps: {
    leave(): Promise<boolean>;
    remove(force: boolean): Promise<{ removed: string } | { error: string }>;
  },
): Promise<{ stayed: true } | { removed: string } | { error: string; dirty: boolean }> {
  if (!(await deps.leave())) return { stayed: true };
  const out = await deps.remove(dirt !== "clean");
  if (isWorktreeError(out)) return { error: out.error, dirty: isDirtyRefusal(out.error) };
  return out;
}

/**
 * THE FOLDER'S WORKTREE LIST, or null when it is not a repo (or not known yet).
 * Every call is numbered and only the NEWEST call's answer for the CURRENT file
 * lands, so an older list arriving late cannot overwrite a newer one. `onReady`
 * hears whether the folder is a repo after every landed answer, and `false`
 * when the hook goes away.
 */
export function useWorktreeList(
  file: string,
  lister: (file: string) => Promise<WorktreeList | { error: string }> = listWorktrees,
  onReady?: (isRepo: boolean) => void,
): { list: WorktreeList | null; refresh: () => Promise<void> } {
  const [list, setList] = useState<WorktreeList | null>(null);
  const live = useRef(true);
  const fileRef = useRef(file);
  fileRef.current = file;
  const gen = useRef(0);
  const listerRef = useRef(lister);
  listerRef.current = lister;
  const readyRef = useRef(onReady);
  readyRef.current = onReady;
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      readyRef.current?.(false);
    };
  }, []);
  const refresh = useCallback(async () => {
    const at = file;
    const g = ++gen.current;
    let next: WorktreeList | null = null;
    try {
      const out = await listerRef.current(at);
      next = isWorktreeError(out) || !(out as WorktreeList).root ? null : (out as WorktreeList);
    } catch {
      next = null;
    }
    if (!live.current || fileRef.current !== at || g !== gen.current) return;
    setList(next);
    readyRef.current?.(!!next);
  }, [file]);
  useEffect(() => {
    setList(null);
    void refresh();
  }, [refresh]);
  return { list, refresh };
}

export interface WorktreeMenuProps {
  /** The chat's `_file` — a FOLDER (the host mounts this for folders only,
   *  keyed on it, so a new folder is a new instance). */
  file: string;
  /** Open `path`'s folder chat, fresh (no session, no run). */
  onSwitch(path: string): void;
  /** A `/worktree [name]` from the composer. */
  request?: WorktreeRequest | null;
  /** A turn is live in this chat: Remove is refused. */
  running?: boolean;
  /** The current worktree was removed. Absent, the chat moves to the main
   *  checkout's folder chat (`onSwitch`); present, the host decides — the
   *  tasks page stays put and re-reads its rows (the folder is now missing). */
  onRemoved?(): void;
  /** Whether this menu can act: true once the list says the folder is in a
   *  repo, false when it is not (or the menu goes away). `/worktree` reads it. */
  onReady?(isRepo: boolean): void;
}

export function WorktreeMenu({
  file,
  onSwitch,
  request,
  running,
  onReady,
  onRemoved,
}: WorktreeMenuProps) {
  const { list, refresh } = useWorktreeList(file, listWorktrees, onReady);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  /** A server refusal said "dirty": the next press forces. */
  const [refusedDirty, setRefusedDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  // AN ANSWER ABOUT A FOLDER THIS CHAT HAS SINCE LEFT IS DROPPED.
  const fileRef = useRef(file);
  fileRef.current = file;
  const stale = (at: string) => !live.current || fileRef.current !== at;

  const closeMenu = useCallback(() => setOpen(false), []);
  useDismissOnWindow(open, closeMenu);
  const onOpenChange = useCallback(
    (next: boolean) => {
      setOpen(next);
      if (next) {
        void refresh();
      } else {
        setCreating(null);
        setConfirming(false);
        setRefusedDirty(false);
        setError("");
      }
    },
    [refresh],
  );

  const go = useCallback(
    (path: string) => {
      setOpen(false);
      setCreating(null);
      setConfirming(false);
      onSwitch(path);
    },
    [onSwitch],
  );

  const onCreate = useCallback(
    async (name: string) => {
      const at = file;
      setBusy(true);
      setError("");
      try {
        const out = await createAndTarget(at, name);
        if (stale(at)) return;
        if (isWorktreeError(out)) setError(out.error);
        else go(out.path);
      } catch (err) {
        if (!stale(at)) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!stale(at)) setBusy(false);
      }
    },
    [file, go],
  );

  const rows = list ? worktreeRows(list) : [];
  const current = rows.find((r) => r.current) ?? null;
  const dirt: DirtyState | undefined = current
    ? refusedDirty
      ? "dirty"
      : dirtyState(current)
    : undefined;
  const onRemove = useCallback(async () => {
    if (!current || !list || running) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }
    const at = file;
    setBusy(true);
    setError("");
    try {
      const out = await removeFlow(dirt ?? "unknown", {
        leave: confirmLeave,
        remove: (force) => removeWorktree(at, current.name, force),
      });
      if (stale(at)) return;
      if ("stayed" in out) return;
      if ("error" in out) {
        setError(out.error);
        // A dirty refusal keeps the confirm up, and the next press forces.
        if (out.dirty) setRefusedDirty(true);
        else setConfirming(false);
      } else if (onRemoved) {
        // The host owns the landing (tasks page: stay, re-read the rows).
        // Closed through `onOpenChange` so the dirty refusal is forgotten too:
        // a menu that stays mounted must not reopen offering a forced remove.
        onOpenChange(false);
        onRemoved();
      } else {
        // Back to the main checkout — on the chat's own file there when it exists.
        const main = list.worktrees.find((w) => w.is_main);
        go(main ? switchTarget(main) : list.root);
      }
    } catch (err) {
      if (!stale(at)) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!stale(at)) setBusy(false);
    }
  }, [confirming, current, dirt, file, go, list, running, onRemoved, onOpenChange]);

  // `/worktree` from the composer: no name opens this menu, a name is
  // create-or-switch — with a refusal shown in the menu it would have used,
  // and the worktree ALREADY open answered by opening the menu.
  const seen = useRef(request?.seq ?? 0);
  useEffect(() => {
    if (!request || request.seq === seen.current) return;
    seen.current = request.seq;
    if (!request.name) {
      onOpenChange(true);
      return;
    }
    const at = file;
    const name = request.name;
    setBusy(true);
    setError("");
    void resolveWorktree(at, name)
      .then((out) => {
        if (stale(at)) return;
        if (isWorktreeError(out)) {
          setError(out.error);
          setOpen(true);
        } else if (out.current) onOpenChange(true);
        else go(out.path);
      })
      .catch((err: unknown) => {
        if (stale(at)) return;
        setError(err instanceof Error ? err.message : String(err));
        setOpen(true);
      })
      .finally(() => {
        if (!stale(at)) setBusy(false);
      });
  }, [request, file, go, onOpenChange]);

  if (!list) return null;
  const label = triggerLabel(list);
  return (
    <div className="chat-worktree-menu">
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger
          render={
            <button
              type="button"
              className="chat-worktree-btn"
              aria-label={"Worktree: " + label}
              title={list.current || list.root}
            >
              <GitBranch size={12} aria-hidden="true" />
              <span>{label}</span>
            </button>
          }
        />
        <PopoverContent
          side="bottom"
          align="end"
          sideOffset={6}
          aria-label="Worktrees"
          className="c-overlay chat-worktree-pop w-auto gap-0 rounded-[10px] bg-[var(--c-panel)] p-1 text-[var(--c-fg)] shadow-none ring-0"
        >
          <WorktreeMenuBody
            rows={rows}
            onSwitch={go}
            creating={creating}
            onCreating={(v) => {
              setCreating(v);
              setError("");
            }}
            onCreate={(name) => void onCreate(name)}
            confirming={confirming}
            {...(dirt ? { dirt } : {})}
            onRemove={() => void onRemove()}
            running={!!running}
            busy={busy}
            error={error}
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}
