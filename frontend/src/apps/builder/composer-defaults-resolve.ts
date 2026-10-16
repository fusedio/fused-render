// The New app composer's model / effort, resolved from the ONE global pair
// (platform/lib/claude-defaults). Its own module so the rule is testable
// without importing the composer's React UI.
import type { ClaudeDefaults } from "@platform/lib/claude-defaults";
import type { DefaultModel, SessionEffort } from "@platform/lib/api";
import { listedModelIn } from "@platform/lib/model-vocab";
import { DEFAULT_EFFORT, DEFAULT_MODEL } from "@apps/claude/ui/composer-defaults";
import { effortsOf, modelsOf } from "@platform/lib/agent-backends";

export interface ComposerPicks {
  model: Exclude<DefaultModel, "">;
  effort: Exclude<SessionEffort, "">;
}

/** Always a concrete pair: the global value when this build offers it, else
 *  the composer's own fallback. `null` (not read yet) is the fallback too.
 *  "Offers" is the agent catalog's CLAUDE row (docs/COPILOT_BACKEND.md D11):
 *  a new app is scaffolded by Claude, whatever the chat default says. */
export function resolveComposerDefaults(
  defaults: ClaudeDefaults | null,
): ComposerPicks {
  const model = listedModelIn(defaults?.model, modelsOf("claude")) || DEFAULT_MODEL;
  const effort = effortsOf("claude").includes(defaults?.effort ?? "")
    ? defaults!.effort
    : DEFAULT_EFFORT;
  return {
    model: model as ComposerPicks["model"],
    effort: effort as ComposerPicks["effort"],
  };
}
