// THE WORKTREE MENU (SPEC W-4) — in the top bar of a FOLDER chat that is inside
// a git repository: which checkout this chat is working in, and the door to the
// others.
//
// SWITCHING IS NAVIGATING. A worktree is just another folder, so moving to one
// is opening that folder's chat with no session — `onSwitch(path)`, which the
// host turns into the same URL the Home lists open a folder chat on
// (`list-rows.paneChatUrl`). Nothing here knows a URL.
//
// The popover is the pills' own (`PillSelect`: shadcn's Popover over Base UI)
// rather than the ⋮'s dropdown, because one of its states is a text field and a
// menu's typeahead would eat the keystrokes.
import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import {
  defaultWorktreeName,
  isWorktreeError,
  listWorktrees,
  createWorktree,
  removeWorktree,
  resolveWorktree,
  triggerLabel,
  validWorktreeName,
  worktreeRows,
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
  onRemove(): void;
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
  onRemove,
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
          onClick={row.current ? undefined : () => onSwitch(row.path)}
        >
          <span className="chat-worktree-name">{row.name}</span>
          <span className="chat-worktree-branch">{row.branch}</span>
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
          disabled={busy}
          onClick={onRemove}
        >
          {confirming
            ? current.dirty
              ? "Has uncommitted changes. Remove anyway"
              : "Confirm remove"
            : "Remove this worktree"}
        </button>
      ) : null}
      {error ? <div className="chat-worktree-note is-error">{error}</div> : null}
    </div>
  );
}

export interface WorktreeMenuProps {
  /** The chat's `_file` — a FOLDER (the host mounts this for folders only). */
  file: string;
  /** Open `path`'s folder chat, fresh (no session, no run). */
  onSwitch(path: string): void;
  /** A `/worktree [name]` from the composer. */
  request?: WorktreeRequest | null;
}

export function WorktreeMenu({ file, onSwitch, request }: WorktreeMenuProps) {
  const [list, setList] = useState<WorktreeList | null>(null);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const out = await listWorktrees(file);
      if (!live.current) return;
      setList(isWorktreeError(out) || !out.root ? null : out);
    } catch {
      if (live.current) setList(null);
    }
  }, [file]);
  useEffect(() => {
    setList(null);
    void refresh();
  }, [refresh]);

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
      setBusy(true);
      setError("");
      try {
        const out = await createWorktree(file, name);
        if (!live.current) return;
        if (isWorktreeError(out)) setError(out.error);
        else go(out.path);
      } catch (err) {
        if (live.current) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (live.current) setBusy(false);
      }
    },
    [file, go],
  );

  const rows = list ? worktreeRows(list) : [];
  const current = rows.find((r) => r.current) ?? null;
  const onRemove = useCallback(async () => {
    if (!current || !list) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setBusy(true);
    setError("");
    try {
      // The second press on a DIRTY worktree is the force the confirm asked for.
      const out = await removeWorktree(file, current.name, current.dirty);
      if (!live.current) return;
      if (isWorktreeError(out)) {
        setError(out.error);
        setConfirming(false);
      } else go(list.root);
    } catch (err) {
      if (live.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (live.current) setBusy(false);
    }
  }, [confirming, current, file, go, list]);

  // `/worktree` from the composer: no name opens this menu, a name is
  // create-or-switch — with a refusal shown in the menu it would have used.
  const seen = useRef(request?.seq ?? 0);
  useEffect(() => {
    if (!request || request.seq === seen.current) return;
    seen.current = request.seq;
    if (!request.name) {
      onOpenChange(true);
      return;
    }
    const name = request.name;
    setBusy(true);
    setError("");
    void resolveWorktree(file, name)
      .then((out) => {
        if (!live.current) return;
        if (isWorktreeError(out)) {
          setError(out.error);
          setOpen(true);
        } else go(out.path);
      })
      .catch((err: unknown) => {
        if (!live.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setOpen(true);
      })
      .finally(() => {
        if (live.current) setBusy(false);
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
            onRemove={() => void onRemove()}
            busy={busy}
            error={error}
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}
