// WHAT A LOCAL SLASH COMMAND DOES, decided as a pure function (SPEC C-1) so the
// refusals can be read and tested at the desk. `ClaudeChat.onLocalCommand`
// carries the verdict out: an action it performs, or a refusal it shows with
// `notify` while the composer keeps the words.
import { LOCAL_COMMANDS, type ClassifiedCommand } from "../protocol/commands";
import { parseWorktreeCommand } from "../protocol/worktrees";
import { EFFORTS, MODELS, MODEL_LABELS } from "./composer-defaults";

/** `/model <name>`'s argument as a model value: a shortlist id or its label
 *  (any case), or a full Claude model id passed through as typed — or "". */
export function localModelArg(arg: string): string {
  const want = arg.trim().toLowerCase();
  if (!want) return "";
  const short = MODELS.find((m) => m === want || (MODEL_LABELS[m] ?? "").toLowerCase() === want);
  if (short) return short;
  return /^claude-[a-z0-9.-]+$/.test(want) ? want : "";
}

/** `/effort <level>`'s argument as a pill value, or "". */
export function localEffortArg(arg: string): string {
  const want = arg.trim().toLowerCase();
  return (EFFORTS as readonly string[]).includes(want) ? want : "";
}

const hint = (name: string) => LOCAL_COMMANDS.find((c) => c.name === name)?.hint ?? "";

export type LocalPlan =
  | { do: "clear" }
  | { do: "tasks" }
  | { do: "model"; value: string }
  | { do: "effort"; value: string }
  | { do: "worktree"; name?: string }
  | { refuse: string };

export interface LocalContext {
  /** A worktree menu is mounted AND has said this folder is in a git repo. */
  worktreeReady: boolean;
}

export function planLocalCommand(cmd: ClassifiedCommand, ctx: LocalContext): LocalPlan {
  switch (cmd.name) {
    case "clear":
    case "help":
    case "tasks":
      // An argument here is words the reader meant to keep (a message pasted
      // after the command): refuse rather than throw them away.
      if (cmd.arg) return { refuse: `/${cmd.name} takes no argument` };
      if (cmd.name === "help") return { refuse: "/help opens the command menu" };
      return { do: cmd.name };
    case "model": {
      const value = localModelArg(cmd.arg);
      if (!value) {
        return { refuse: `Usage: /model ${hint("model")} (${MODELS.join(", ")}, or a full claude-… model id)` };
      }
      return { do: "model", value };
    }
    case "effort": {
      const value = localEffortArg(cmd.arg);
      if (!value) return { refuse: `Usage: /effort ${hint("effort")} (one of ${EFFORTS.join(", ")})` };
      return { do: "effort", value };
    }
    case "worktree": {
      if (!ctx.worktreeReady) {
        return { refuse: "/worktree works in a folder chat inside a git repository" };
      }
      const parsed = parseWorktreeCommand(cmd.arg);
      if ("error" in parsed) return { refuse: `Usage: /worktree ${hint("worktree")}. ${parsed.error}` };
      return "name" in parsed ? { do: "worktree", name: parsed.name } : { do: "worktree" };
    }
    default:
      return { refuse: `/${cmd.name} is not a local command` };
  }
}
