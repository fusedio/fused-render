// "WORK MOVED INTO A WORKTREE" — the header chip beside the worktree menu.
//
// The menu names the checkout THIS chat is working in. The model can quietly
// move elsewhere: `git worktree add …` in Bash, the EnterWorktree tool, an
// Agent started with `isolation: "worktree"`. The poll's `worktree_hint`
// (agent.py `_worktree_hint`) says so; this draws it, unless the chat is
// already in that folder (then the menu has said it).
import { GitBranch } from "lucide-react";

import { isLive } from "../protocol/tasks";
import type { TaskRecord, WorktreeHint } from "../protocol/types";

const trim = (p: string): string => (p.length > 1 ? p.replace(/\/+$/, "") : p);
const base = (p: string): string => trim(p).split("/").filter(Boolean).pop() ?? "";

/** The hint's folder IS the chat's folder. The server sends absolute paths,
 *  compared exactly. A relative one (a legacy hint, or a call with no known
 *  cwd) matches only as the chat folder's own name (`p`) or as a trailing part
 *  of its path (`./a/p`), and never when it climbs with `..`. */
export function isSameFolder(hintPath: string, currentPath?: string): boolean {
  if (!hintPath || !currentPath) return false;
  const h = trim(hintPath);
  const c = trim(currentPath);
  if (h.startsWith("/")) return h === c;
  if (h.split("/").includes("..")) return false;
  const rel = h.replace(/^\.\//, "");
  return rel !== "" && (rel === base(c) || c.endsWith("/" + rel));
}

/** A `via: "agent"` hint is true only while an isolated agent is running:
 *  hidden once no task with `isolation === "worktree"` is live. */
export function visibleWorktreeHint(hint: WorktreeHint | null | undefined, tasks: TaskRecord[] | undefined): WorktreeHint | null {
  if (!hint) return null;
  if (hint.via !== "agent") return hint;
  return (tasks ?? []).some((t) => t.isolation === "worktree" && isLive(t.status)) ? hint : null;
}

const HOW: Record<WorktreeHint["via"], string> = {
  bash: "created with git worktree add",
  enter: "entered",
  agent: "a subagent's isolated worktree",
  exit: "left the worktree",
};

export function WorktreeHintChip({ hint, currentPath }: { hint: WorktreeHint | null; currentPath?: string }) {
  if (!hint) return null;
  if (isSameFolder(hint.path, currentPath)) return null;
  if (hint.via === "exit") return null;
  const name = hint.branch || base(hint.path) || hint.label || (hint.via === "agent" ? "subagent" : "");
  const title = [hint.path && "Path: " + hint.path, hint.branch && "Branch: " + hint.branch, HOW[hint.via]]
    .filter(Boolean)
    .join("\n");
  return (
    <span className="chat-worktree-btn chat-worktree-hint" title={title}>
      <GitBranch size={12} aria-hidden="true" />
      <span>{name ? "worktree · " + name : "worktree"}</span>
    </span>
  );
}

export default WorktreeHintChip;
