// The New app composer's model / effort, resolved from the ONE global pair
// (platform/lib/claude-defaults). Its own module so the rule is testable
// without importing the composer's React UI.
import type { ClaudeDefaults } from "@platform/lib/claude-defaults";
import type { DefaultModel, SessionEffort } from "@platform/lib/api";
import { listedModelIn } from "@platform/lib/model-vocab";
import {
  DEFAULT_EFFORT,
  DEFAULT_MODEL,
  EFFORTS,
  MODELS,
} from "@apps/claude/ui/composer-defaults";

export interface ComposerPicks {
  model: Exclude<DefaultModel, "">;
  effort: Exclude<SessionEffort, "">;
}

/** Always a concrete pair: the global value when this build offers it, else
 *  the composer's own fallback. `null` (not read yet) is the fallback too. */
export function resolveComposerDefaults(
  defaults: ClaudeDefaults | null,
): ComposerPicks {
  const model = listedModelIn(defaults?.model, MODELS) || DEFAULT_MODEL;
  const effort = (EFFORTS as readonly string[]).includes(defaults?.effort ?? "")
    ? defaults!.effort
    : DEFAULT_EFFORT;
  return {
    model: model as ComposerPicks["model"],
    effort: effort as ComposerPicks["effort"],
  };
}
