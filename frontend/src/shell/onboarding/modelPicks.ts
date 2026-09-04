// Which models the wizard's Models step offers, and which of them start
// checked — the part with a RULE in it, kept out of the .tsx for the reason
// `apps/ai_models/shared/fitNote.ts` gives about itself: there is no DOM
// harness in this repo, so a decision a reader has to trust lives in a module
// that can be read (and driven) on its own.
//
// The offer is ONE model per capability: the curated `recommended` row
// (catalog.py's second curation axis, D425 — "the one this app stands behind"),
// never the capability's `default`, which is the smallest entry and exists to
// answer "just load something". A capability whose engine cannot run here
// (`available: false`) offers nothing: a Download that would 409 is worse than
// a row that is not there.
//
// **Video is excluded by id, not by curation.** `ltx-2.3-mlx-q4` is the
// recommended video model and it is a 28.5 GB download — one entry that
// dwarfs the other four put together (~9 GB). Onboarding is not where someone
// decides to spend that, and a total reading "38 GB" makes the step look like
// a demand rather than a head start. It stays one click away on /ai-models.
import type { AiCatalogCapability, AiCatalogModel } from "@platform/lib/api";

/** `registry.VIDEO_GENERATION`. Left out of the step — see the header. */
const EXCLUDED_CAPABILITY = "text-to-video";

export interface ModelPick {
  capability: string;
  /** What the model DOES, in words — the caption beside its name. */
  capabilityLabel: string;
  runnerShortLabel: string | null;
  model: AiCatalogModel;
}

/** What the step offers, in the catalog's own order (the server decides which
 *  capability comes first; a second ordering here could only disagree with
 *  it). Empty when nothing on this machine can run anything — the signal the
 *  wizard uses to drop the step entirely. */
export function modelPicks(capabilities: AiCatalogCapability[]): ModelPick[] {
  const picks: ModelPick[] = [];
  for (const cap of capabilities) {
    if (cap.capability === EXCLUDED_CAPABILITY) continue;
    if (!cap.available) continue;
    const model = cap.models.find((m) => m.recommended);
    if (!model) continue;
    picks.push({
      capability: cap.capability,
      capabilityLabel: capabilityLabel(cap.capability),
      runnerShortLabel: cap.runnerShortLabel,
      model,
    });
  }
  return picks;
}

/** The capability in words. The server's own `runnerLabel` names the BACKEND
 *  ("MLX LM (Apple Silicon)"), which is not what a first-run reader needs;
 *  what they need is what the model is FOR. */
function capabilityLabel(capability: string): string {
  switch (capability) {
    case "text-generation":
      return "Chat and writing";
    case "text-to-image":
      return "Image generation";
    case "automatic-speech-recognition":
      return "Speech to text";
    case "embeddings":
      return "Search and similarity";
    default:
      return capability;
  }
}

/** The ids that start checked: it fits comfortably (`fit.verdict === "easy"`,
 *  fit.py's own word for "at or under the comfort utilization") and it is not
 *  already here.
 *
 *  A `tight`/`no` verdict is OFFERED but not preselected, and a null fit —
 *  nothing known about the footprint, so nothing to judge — is not
 *  preselected either, the same no-guess rule `fitNote` follows by drawing no
 *  badge at all. Nothing here DISABLES a row: /ai-models greys a Download
 *  only when no engine can serve it (RecommendedCard.tsx), never on a fit
 *  verdict, and two surfaces disagreeing about whether a big model may be
 *  fetched is worse than a badge saying it will be tight. */
export function comfortableIds(picks: ModelPick[]): string[] {
  return picks
    .filter((p) => !p.model.downloaded && p.model.fit?.verdict === "easy")
    .map((p) => p.model.id);
}

/** The download total for `ids`, in bytes, and how many of them said. A model
 *  with no `size_gb` contributes nothing and is counted in `unknown`, so the
 *  button can say "~9 GB" honestly rather than folding a guess into the
 *  figure (AI-11a's no-invented-size rule). */
export function selectedTotal(
  picks: ModelPick[],
  ids: Iterable<string>,
): { bytes: number; count: number; unknown: number } {
  const wanted = new Set(ids);
  let bytes = 0;
  let count = 0;
  let unknown = 0;
  for (const p of picks) {
    if (!wanted.has(p.model.id)) continue;
    count += 1;
    if (p.model.size_gb == null) unknown += 1;
    else bytes += p.model.size_gb * 1e9;
  }
  return { bytes, count, unknown };
}
