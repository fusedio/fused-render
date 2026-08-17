// How the Local tab's cards are BUCKETED, separated from what draws them — the
// same split, and for the same reason, as `engines.ts` beside it (D302).
//
// The listing arrives as one flat run sorted by size, and the sort is the whole
// problem: a 2.4GB repo a runner fetched for itself lands fifth, between two
// models the user chose, and the only thing distinguishing it is the quietest
// element on the card. Position carried no meaning at all. What decides a
// bucket is a two-field question over `component` and `capability`, it has a
// wrong answer that looks right (see `UNRECOGNISED`), and none of that is
// visible in a screenshot — so it lives here as plain functions, with
// `aiModelGroups.test.ts` driving them.
//
// Client side by decision: every field this reads is already in the payload,
// and a `group` field on the response would be the server deciding a layout
// question for one page.
import type { AiModelRepo } from "@platform/lib/api";
import { capabilityLabel } from "@shell/engines";

/** The bucket for a repo with no capability AND no component.
 *
 *  Not a capability, so it cannot collide with one — the server's vocabulary is
 *  the Hub's tags, which are all hyphenated lowercase words.
 */
export const UNRECOGNISED = "unrecognised";

/** The reading order for the capabilities this app actually serves.
 *
 *  Hardcoded, and only for ORDER — every label still comes from
 *  `capabilityLabel`, so there is one place where a capability is put into
 *  words. A capability missing from this list is not missing from the page: it
 *  sorts after these, in the order the server sent it (see `groupRepos`), which
 *  is how a capability added server-side shows up here without a frontend
 *  change.
 */
const CAPABILITY_ORDER = ["text-generation", "text-to-image", "automatic-speech-recognition"];

export interface RepoGroup {
  /** The capability tag, or `UNRECOGNISED`. */
  key: string;
  label: string;
  /** What this heading has to explain that its label cannot, or null. */
  note: string | null;
  repos: AiModelRepo[];
  /** Bytes on disk across `repos`. */
  size: number;
}

export interface GroupedRepos {
  /** `component === null` — what somebody chose to download, by capability. */
  models: { groups: RepoGroup[]; size: number };
  /** `component !== null` — what a runner fetched to do its job. Not
   *  sub-grouped: there are a handful, and the section heading is what does the
   *  work now that the cards are no longer scattered through the list. */
  components: { repos: AiModelRepo[]; size: number };
}

/** The one group whose heading has to argue for itself.
 *
 *  This is where the abandoned diarization embedding repo lands — no capability,
 *  no engine, no owner. Without a group of its own it renders as `no engine`,
 *  which is the SAME tag a 4.6GB model the user deliberately downloaded wears,
 *  and there was no vocabulary anywhere on the page for "we do not know what
 *  this is".
 */
const UNRECOGNISED_NOTE = "Cannot be loaded into Fused Render because the model type is not supported.";

function totalSize(repos: AiModelRepo[]): number {
  return repos.reduce((bytes, repo) => bytes + repo.size, 0);
}

/** The Local tab's two sections, from the listing's own order.
 *
 *  **Bucketed on `component` and `capability`, never on `engine`.** That is the
 *  distinction this whole grouping exists to draw, and the wrong version passes
 *  a casual look: `mlx-community/Qwen3-8B-4bit` has `capability:
 *  "text-generation"` with `engine: null`, so bucketing on a null engine files
 *  it beside a repo nothing can identify. The app knows exactly what that model
 *  is — it simply cannot load the format — and it belongs under Text generation
 *  wearing its `no engine` tag.
 *
 *  Repo order inside a group is the listing's, which the server already sorted
 *  size-descending then by id. Partitioning preserves it, so there is no second
 *  copy of that rule here to drift from `_listing`'s.
 */
export function groupRepos(repos: AiModelRepo[]): GroupedRepos {
  const components: AiModelRepo[] = [];
  // Insertion-ordered, which is what gives an unknown capability its place:
  // first appearance in the listing, i.e. whatever order the server hands back.
  const byCapability = new Map<string, AiModelRepo[]>();

  for (const repo of repos) {
    if (repo.component) {
      components.push(repo);
      continue;
    }
    const key = repo.capability ?? UNRECOGNISED;
    const bucket = byCapability.get(key);
    if (bucket) bucket.push(repo);
    else byCapability.set(key, [repo]);
  }

  const rank = (key: string) => {
    // Unrecognised is LAST, past every capability known and unknown: it is the
    // section a reader should reach having already found what they came for.
    if (key === UNRECOGNISED) return Number.MAX_SAFE_INTEGER;
    const known = CAPABILITY_ORDER.indexOf(key);
    return known === -1 ? Number.MAX_SAFE_INTEGER - 1 : known;
  };

  // A subgroup exists only if something is in it — `byCapability` holds no
  // empty buckets — so an empty heading is not something the page has to guard
  // against downstream.
  const groups: RepoGroup[] = [...byCapability.entries()]
    .map(([key, members]) => ({
      key,
      label: key === UNRECOGNISED ? "Unrecognised" : capabilityLabel(key),
      note: key === UNRECOGNISED ? UNRECOGNISED_NOTE : null,
      repos: members,
      size: totalSize(members),
    }))
    // Stable, so two capabilities the order list does not name keep the
    // listing's relative order rather than swapping under the sort.
    .sort((a, b) => rank(a.key) - rank(b.key));

  return {
    models: { groups, size: totalSize(groups.flatMap((g) => g.repos)) },
    components: { repos: components, size: totalSize(components) },
  };
}

/** What `no engine` MEANS on this repo — the tag's hover, and the first half of
 *  the Load refusal below.
 *
 *  **Two different nothings wear the same tag**, and blaming the FORMAT for both
 *  is what put the Wespeaker orphan and a perfectly identifiable Qwen checkpoint
 *  behind one sentence. With a capability we know what the repo is FOR and the
 *  format really is the obstacle; without one we know nothing about it at all,
 *  and "no engine reads this format" implies a diagnosis nobody made.
 *
 *  It lives here, next to `UNRECOGNISED_NOTE`, because three surfaces say this
 *  about the same card — the group heading, the Load refusal, and the tag's
 *  hover — and the hover was the one that kept the format sentence after the
 *  other two stopped saying it. One function, so a card cannot contradict
 *  itself again.
 */
export function noEngineReason(repo: AiModelRepo): string {
  if (repo.capability === null) {
    // Agrees with the Unrecognised heading above the card, which is the only
    // other place on the page that has an opinion about this repo.
    return UNRECOGNISED_NOTE;
  }
  return (
    "No local engine reads this repo's weight format. The formats are not " +
    "interchangeable — a Whisper repo comes as CTranslate2, MLX or " +
    "transformers, and each engine loads exactly one of them."
  );
}

/** Why Load is refused for this repo, or null when it can be loaded.
 *
 *  **Every card offers Load, always.** A control that vanishes teaches nothing:
 *  a user comparing two cards cannot tell "this model cannot be loaded" from "I
 *  misremembered where the button was", and a row whose width changes card to
 *  card is one the eye never learns to read. So the button is always there, and
 *  the state rides on `disabled` plus this sentence.
 *
 *  Which makes the sentence the whole feature — a disabled button with no
 *  explanation is the same dead end as a missing one. The four refusals are four
 *  different problems with four different fixes, and one flat "cannot load"
 *  would send all of them nowhere.
 */
export function loadRefusal(repo: AiModelRepo): string | null {
  if (repo.component) {
    // The consequence of deleting it is on the tag's own hover; what the BUTTON
    // has to say is why there is nothing to load, which is that this was never
    // a model. Naming the owner is what makes that checkable by the reader.
    return (
      `Part of ${repo.component.owner}, not a model — a ${repo.component.part} ` +
      "this app downloaded for it. There is nothing here to load."
    );
  }
  if (repo.kind !== "model") {
    // A dataset or a Space in the same cache. Says what it IS rather than
    // blaming an engine, because no engine was ever going to be the answer.
    return `This is a ${repo.kind}, not a model — nothing here loads one.`;
  }
  if (!repo.engine) {
    // The same sentence the tag's hover shows, because they are answering the
    // same question about the same card. What the BUTTON adds is the one clause
    // that is about loading: an unrecognised repo has no capability to be loaded
    // AS, which is a dead end the format sentence does not have — a repo whose
    // format nothing reads is still a text model, and the answer there is an
    // engine or another copy of the weights.
    if (repo.capability === null) {
      return noEngineReason(repo) + " There is nothing to load it as.";
    }
    return noEngineReason(repo);
  }
  if (!repo.engine.available) {
    // The registry's own sentence, quoted rather than paraphrased. It is the
    // only copy of WHICH thing is in the way — a platform on one machine, an
    // engine preference on another — and this page cannot synthesise either.
    return (
      `This is a ${repo.engine.shortLabel} model, and it cannot be loaded here: ` +
      `${repo.engine.reason ?? "unavailable"}.`
    );
  }
  return null;
}
