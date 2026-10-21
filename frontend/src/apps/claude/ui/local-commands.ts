// WHAT A LOCAL SLASH COMMAND DOES, decided as a pure function (SPEC C-1) so the
// refusals can be read and tested at the desk. `ClaudeChat.onLocalCommand`
// carries the verdict out: an action it performs, or a refusal it shows with
// `notify` while the composer keeps the words.
import { LOCAL_COMMANDS, type ClassifiedCommand } from "../protocol/commands";
import { parseWorktreeCommand } from "../protocol/worktrees";
import { agentLabel, capsOf, effortsOf, modelLabelsOf, modelsOf } from "@platform/lib/agent-backends";

/** `/model <name>`'s argument as a model value: a shortlist id or its label
 *  (any case) off the AGENT's catalog row (docs/COPILOT_BACKEND.md D11), or a
 *  full model id passed through as typed — or "". Claude's list when no
 *  agent is named, which is the static row until the catalog lands. */
export function localModelArg(arg: string, backend = "claude"): string {
  const want = arg.trim().toLowerCase();
  if (!want) return "";
  const labels = modelLabelsOf(backend);
  const short = modelsOf(backend).find((m) => m === want || (labels[m] ?? "").toLowerCase() === want);
  if (short) return short;
  const full = backend === "copilot" ? /^[a-z][a-z0-9.-]+$/ : /^claude-[a-z0-9.-]+$/;
  return full.test(want) ? want : "";
}

/** `/effort <level>`'s argument as a pill value, or "". */
export function localEffortArg(arg: string, backend = "claude"): string {
  const want = arg.trim().toLowerCase();
  return effortsOf(backend).includes(want) ? want : "";
}

const hint = (name: string) => LOCAL_COMMANDS.find((c) => c.name === name)?.hint ?? "";

export type LocalPlan =
  | { do: "clear" }
  | { do: "tasks" }
  | { do: "model"; value: string }
  | { do: "effort"; value: string }
  | { do: "worktree"; name?: string }
  | { do: "remote-control"; name: string }
  | { refuse: string };

export interface LocalContext {
  /** A worktree menu is mounted AND has said this target is in a git repo. */
  worktreeReady: boolean;
  /** Whose models `/model` and `/effort` validate against; Claude when absent. */
  backend?: string;
  hasSession?: boolean;
}

export function planLocalCommand(cmd: ClassifiedCommand, ctx: LocalContext): LocalPlan {
  switch (cmd.name) {
    case "clear":
    case "help":
    case "tasks":
      // No background tasks to show for an agent without them (§7 `bg_tasks`).
      if (cmd.name === "tasks" && !capsOf(ctx.backend).bg_tasks) {
        return { refuse: `${agentLabel(ctx.backend)} has no background tasks` };
      }
      // An argument here is words the reader meant to keep (a message pasted
      // after the command): refuse rather than throw them away.
      if (cmd.arg) return { refuse: `/${cmd.name} takes no argument` };
      if (cmd.name === "help") return { refuse: "/help opens the command menu" };
      return { do: cmd.name };
    case "model": {
      const value = localModelArg(cmd.arg, ctx.backend);
      if (!value) {
        const full = ctx.backend === "copilot" ? "a full model id" : "a full claude-… model id";
        return { refuse: `Usage: /model ${hint("model")} (${modelsOf(ctx.backend).join(", ")}, or ${full})` };
      }
      return { do: "model", value };
    }
    case "effort": {
      const value = localEffortArg(cmd.arg, ctx.backend);
      if (!value) return { refuse: `Usage: /effort ${hint("effort")} (one of ${effortsOf(ctx.backend).join(", ")})` };
      return { do: "effort", value };
    }
    case "worktree": {
      if (!capsOf(ctx.backend).worktrees) return { refuse: `${agentLabel(ctx.backend)} has no worktrees` };
      if (!ctx.worktreeReady) {
        return { refuse: "/worktree works in a chat on a file or folder inside a git repository" };
      }
      const parsed = parseWorktreeCommand(cmd.arg);
      if ("error" in parsed) return { refuse: `Usage: /worktree ${hint("worktree")}. ${parsed.error}` };
      return "name" in parsed ? { do: "worktree", name: parsed.name } : { do: "worktree" };
    }
    case "remote-control":
      if ((ctx.backend || "claude") !== "claude") {
        return { refuse: `Remote Control is a Claude Code feature, not available for ${agentLabel(ctx.backend)}` };
      }
      if (!ctx.hasSession) return { refuse: "Send a first message, then turn on Remote Control" };
      return { do: "remote-control", name: cmd.arg };
    default:
      return { refuse: `/${cmd.name} is not a local command` };
  }
}
