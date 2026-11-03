// The New app composer's model / effort, resolved from the ONE global pair
// (platform/lib/claude-defaults) for Claude, and from the agent's own catalog
// row for anyone else. Its own module so the rule is testable without
// importing the composer's React UI.
import type { ClaudeDefaults } from "@platform/lib/claude-defaults";
import type { SessionEffort } from "@platform/lib/api";
import { listedModelIn } from "@platform/lib/model-vocab";
import { DEFAULT_EFFORT, DEFAULT_MODEL } from "@apps/claude/ui/composer-defaults";
import { defaultEffortFor, defaultModelOf, effortsOf, modelsOf } from "@platform/lib/agent-backends";

export interface ComposerPicks {
  /** A concrete model id on the agent's own list — never "". */
  model: string;
  /** The effort the pair's model takes, or "" for a model that takes none
   *  (a Copilot model whose catalog row lists no levels): then no flag goes
   *  on the wire and the composer draws no effort pick. One vocabulary for
   *  every agent (`SessionEffort`, the CLI flag's values). */
  effort: SessionEffort;
}

/** Always a concrete model. For Claude: the global value when this build
 *  offers it, else the composer's own fallback (`null`, not read yet, is the
 *  fallback too). "Offers" is the agent catalog's Claude row
 *  (docs/COPILOT_BACKEND.md D11). For ANOTHER agent the global pair is
 *  Claude's and does not apply (the New task card's rule, NewJobModal): the
 *  model is that agent's `default_model` (`chosen`, when given, is the one
 *  already picked) and the effort is THAT MODEL's default — the CLI's own
 *  when the catalog carries it, "medium" when the model takes it, its
 *  first level, or "" when it takes none. */
export function resolveComposerDefaults(
  defaults: ClaudeDefaults | null,
  agent: string = "claude",
  chosen?: string,
): ComposerPicks {
  if ((agent || "claude") !== "claude") {
    const models = modelsOf(agent);
    const want = chosen || defaultModelOf(agent);
    const picked = (models.includes(want) ? want : models[0]) || want || DEFAULT_MODEL;
    return {
      model: picked,
      effort: defaultEffortFor(agent, picked) as ComposerPicks["effort"],
    };
  }
  const model = listedModelIn(defaults?.model, modelsOf("claude")) || DEFAULT_MODEL;
  const effort = effortsOf("claude").includes(defaults?.effort ?? "")
    ? defaults!.effort
    : DEFAULT_EFFORT;
  return { model, effort: effort as ComposerPicks["effort"] };
}
