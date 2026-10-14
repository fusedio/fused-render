// "WORK MOVED INTO A WORKTREE" — the header chip beside the worktree menu.
//
// The menu names the checkout THIS chat is working in. The model can quietly
// move elsewhere: `git worktree add …` in Bash, the EnterWorktree tool, an
// Agent started with `isolation: "worktree"`. The poll's `worktree_hint`
// (agent.py `_worktree_hint`) says so; this draws it, unless the chat is
// already in that folder (then the menu has said it).
import { GitBranch } from "lucide-react";

import type { WorktreeHint } from "../protocol/types";

const trim = (p: string): string => (p.length > 1 ? p.replace(/\/+$/, "") : p);
const base = (p: string): string => trim(p).split("/").filter(Boolean).pop() ?? "";

/** The hint's folder IS the chat's folder: same absolute path, or a relative
 *  path that is just the chat folder's own basename. */
export function isSameFolder(hintPath: string, currentPath?: string): boolean {
  if (!hintPath || !currentPath) return false;
  const h = trim(hintPath);
  const c = trim(currentPath);
  if (h.startsWith("/")) return h === c;
  return h === base(c) || c.endsWith("/" + h.replace(/^\.\//, ""));
}

const HOW: Record<WorktreeHint["via"], string> = {
  bash: "created with git worktree add",
  enter: "entered",
  agent: "a subagent's isolated worktree",
};

export function WorktreeHintChip({ hint, currentPath }: { hint: WorktreeHint | null; currentPath?: string }) {
  if (!hint) return null;
  if (isSameFolder(hint.path, currentPath)) return null;
  const name = hint.branch || base(hint.path) || hint.label || "subagent";
  const title = [hint.path && "Path: " + hint.path, hint.branch && "Branch: " + hint.branch, HOW[hint.via]]
    .filter(Boolean)
    .join("\n");
  return (
    <span className="chat-worktree-btn chat-worktree-hint" title={title}>
      <GitBranch size={12} aria-hidden="true" />
      <span>{"worktree · " + name}</span>
    </span>
  );
}

export default WorktreeHintChip;
