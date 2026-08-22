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
import type { AiCatalogCapability, AiCatalogModel, AiModelRepo } from "@platform/lib/api";
import { capabilityLabel } from "@apps/ai_models/lib/engines";

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
 *
 *  Embeddings sits LAST of the four, the same place it sits in
 *  `engines.CAPABILITY_LABELS`: it is the one capability whose output is not
 *  something a reader looks at, so it belongs behind the three that produce
 *  text, an image and a transcript. Listed rather than left to fall through,
 *  because a first-class capability that sorted itself by the accident of
 *  listing order would land in a different place on two machines.
 */
const CAPABILITY_ORDER = [
  "text-generation",
  "text-to-image",
  "automatic-speech-recognition",
  "embeddings",
];

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

/** The tag a partly downloaded repo wears, in place of an engine tag.
 *
 *  Exported because two surfaces say it — the card's tag and its own tests —
 *  and a second copy of the words is a second thing to keep in step.
 */
export const PARTIAL_TAG = "partly downloaded";

/** What "partly downloaded" MEANS on this card: the tag's hover, and the reason
 *  the Load button is not the control this state offers (D424).
 *
 *  **It says what to DO, because this is the one card state with two ways out
 *  and no obvious one.** The bytes on disk are worth something — Download picks
 *  the fetch up where it stopped rather than starting over — and the trash is
 *  what a reader who does not want the model at all is looking for. A sentence
 *  that only diagnosed ("this download did not finish") would leave the reader
 *  where the old `no engine` tag left them: correct, and stuck.
 */
export function partialNote(repo: AiModelRepo): string {
  return (
    `${repo.id} is a download that did not finish. Download picks it up from the ` +
    "bytes already here rather than starting over; the trash discards them."
  );
}

/** Whether this card is the "partly downloaded" one — the state that replaces
 *  the engine tag with `PARTIAL_TAG` and the Load button with a Download.
 *
 *  A COMPONENT is excluded even when its own fetch was interrupted, and that is
 *  not an oversight. A component is nobody's `load()` target and nobody's
 *  Download either (AI-7e): the engine that wanted it re-fetches it on its next
 *  bring-up, so "part of MLX Whisper" stays the more useful thing to read in
 *  front of a delete than an offer to finish a download the user never started.
 *  The kind check is belt-and-braces — the server only ever sets `partial` on a
 *  model — and it keeps this function true on its own terms.
 */
export function resumable(repo: AiModelRepo): boolean {
  return repo.partial && !repo.component && repo.kind === "model";
}

function totalSize(repos: AiModelRepo[]): number {
  return repos.reduce((bytes, repo) => bytes + repo.size, 0);
}

/** Where a capability sorts. Module level because `mergeSections` sorts the
 *  SAME keys as `groupRepos` — a section that exists only because the catalog
 *  recommends something has to land in the same place it would have landed had
 *  a repo for it been on disk, and two copies of this would be two orders. */
function rank(key: string): number {
  // Unrecognised is LAST, past every capability known and unknown: it is the
  // section a reader should reach having already found what they came for.
  if (key === UNRECOGNISED) return Number.MAX_SAFE_INTEGER;
  const known = CAPABILITY_ORDER.indexOf(key);
  return known === -1 ? Number.MAX_SAFE_INTEGER - 1 : known;
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

/** Which backend serves a capability on this machine, from the catalog. */
export interface SectionRunner {
  /** The backend without its hardware qualifier — "MLX LM", "Diffusers". Null
   *  when the catalog resolved no runner at all for the capability. */
  shortLabel: string | null;
  available: boolean;
  /** Why not, when it is not. The registry's own sentence. */
  reason: string | null;
}

/** One capability's row on the Local tab: what this disk HAS, then what to get.
 *
 *  The two halves are different objects on purpose — a disk row is an
 *  `AiModelRepo` the walk measured, a recommended row is a catalog entry nobody
 *  has downloaded — and flattening them into one shape would mean inventing
 *  every field one of them cannot answer (a revision count for a model that is
 *  not here, a curation note for one that is). They are one ROW on screen and
 *  two kinds of card in it.
 */
export interface MergedSection {
  /** The capability tag, or `UNRECOGNISED`. */
  key: string;
  label: string;
  /** What this heading has to explain that its label cannot, or null. */
  note: string | null;
  /** Repos on this disk: loaded first, then most recently used first. */
  disk: AiModelRepo[];
  /** Curated models this disk does NOT have, in the catalog's order, always
   *  after the disk rows. */
  recommended: AiCatalogModel[];
  /** Bytes ON DISK. Recommended entries are not counted and must not be: the
   *  figure beside a heading is a claim about this machine (D249/D251), and a
   *  number that included models nobody has downloaded would be the one fact on
   *  the page that could not be checked against the disk. */
  size: number;
  /** The catalog's verdict for this capability, or null for a section the
   *  catalog has no entry for — `UNRECOGNISED`, and any capability only this
   *  disk knows about. A section with no runner has no recommended rows by
   *  construction, since both come from the same catalog entry. */
  runner: SectionRunner | null;
}

/** The disk rows in the row's reading order: resident first, then by recency.
 *
 *  Only membership of `loadedById` is read, never the row itself: what "loaded"
 *  means for an ORDER is "this one is costing memory right now", and a model
 *  whose weights are still going in is already that. Waiting for `ready` would
 *  move the card twice for one event.
 *
 *  Behind the resident one, MOST RECENTLY USED first — a horizontal row is read
 *  a few cards deep and then scrolled, so the front has to hold the models the
 *  user actually reaches for; the listing's size order optimises for a question
 *  ("what is the disk spent on") the header's byte figure already answers.
 *  `lastUsed` is filesystem atime and can be null (noatime volumes) — nulls sort
 *  last, and ties keep the listing's order, so a volume that never writes atime
 *  degrades to exactly the old sort rather than to a shuffle.
 */
function orderDisk(repos: AiModelRepo[], loaded: ReadonlyMap<string, unknown>): AiModelRepo[] {
  // Finite sentinels, not ±Infinity: two nulls (or, defensively, two resident
  // rows) must compare EQUAL, and Infinity - Infinity is NaN — which a sort
  // comparator reads as garbage, not as a tie.
  const recency = (repo: AiModelRepo) => {
    if (loaded.has(repo.id)) return Number.MAX_SAFE_INTEGER;
    return repo.lastUsed ?? -1;
  };
  // Sorted copy; Array.prototype.sort is stable, which is what "ties keep the
  // listing's order" rests on.
  return [...repos].sort((a, b) => recency(b) - recency(a));
}

/** id → path for every model that already has a DISK CARD on this page.
 *
 *  **A MATERIALISED snapshot OR an unfinished download, which is two conditions
 *  where there used to be one (D424).** `huggingface_hub` creates
 *  `models--org--name/` on the first byte, so folder names alone flipped a
 *  suggestion to "downloaded" seconds after Download was pressed — that is the
 *  trap the revision count was added to close, and it stays closed here.
 *
 *  What the revision count could not see is that a snapshot is materialised FILE
 *  BY FILE: a download cancelled halfway has a revision, no weights, and no
 *  engine that reads it, so it claimed the same "you already have this" as a
 *  finished one — the recommendation with its working Download button
 *  disappeared and left a card that could not be loaded. The repo is NOT a model
 *  this machine has (the server's `partial` says so, and `cached_models()` drops
 *  it, so no picker offers it) — but it IS a card on screen, wearing its own
 *  state and carrying its own Download. Recommending the same model beside it
 *  would draw one model twice, which is what this map exists to prevent.
 *
 *  So the disk card is the one that survives at every stage of a download's
 *  life, and the recommendation returns when the partial repo is DELETED — the
 *  trash on that same card, the second of its two ways out.
 *
 *  It is also what settles a held Download click (`spokenFor`): a card appearing
 *  for the model is something other than the button speaking for the pull, and
 *  that has to be true of a pull that was cancelled as well as one that landed,
 *  or the click stays held over a recommendation that comes back later.
 */
export function cardedOnDisk(repos: AiModelRepo[]): Map<string, string> {
  return new Map(
    repos.filter((r) => r.revisions > 0 || r.partial).map((r) => [r.id, r.path]),
  );
}

/** The Local tab's capability rows: disk and curation in one order.
 *
 *  **A capability with nothing on disk still gets a row when something is
 *  recommended for it**, and that is the whole point of merging rather than
 *  stacking two grids. A fresh machine's Local tab used to be one sentence and a
 *  button to another tab; now the answer to "what should I get" is drawn where
 *  the answer to "what do I have" will appear, in the same row, so the page
 *  fills up in place instead of switching views.
 *
 *  Recommended entries are the CURATED half only, minus anything that already
 *  has a card — `cardedOnDisk`, the page's own walk and not the catalog's
 *  `downloaded` flag: two definitions of "downloaded" on one page are two
 *  moments they were true, and this page has cards drawn from both halves side
 *  by side. Note that the filter is "has a card", not "is downloaded": a partly
 *  downloaded repo is neither, and it keeps its card and its own Download rather
 *  than being recommended a second time beside itself (D424). While the walk has
 *  not answered (`onDisk === null`) nothing is recommended at all — the same
 *  posture the download cards take, since a recommendation is a claim that this
 *  machine does not have the model.
 *
 *  Client side for the reason `groupRepos` above is: which rows sit in which
 *  order is a question about this page's layout, and every field it reads is
 *  already in two payloads the page has.
 */
export function mergeSections(
  groups: RepoGroup[],
  catalog: AiCatalogCapability[] | null,
  loadedById: ReadonlyMap<string, unknown>,
  onDisk: ReadonlyMap<string, string> | null,
): MergedSection[] {
  // First entry wins, so a catalog that ever listed a capability twice cannot
  // recommend the same model twice under one heading.
  const byCapability = new Map<string, AiCatalogCapability>();
  for (const entry of catalog ?? []) {
    if (!byCapability.has(entry.capability)) byCapability.set(entry.capability, entry);
  }

  const runnerOf = (entry: AiCatalogCapability | undefined): SectionRunner | null =>
    entry ? { shortLabel: entry.runnerShortLabel, available: entry.available, reason: entry.reason } : null;

  const recommendedFor = (key: string): AiCatalogModel[] => {
    if (!onDisk) return [];
    const entry = byCapability.get(key);
    if (!entry) return [];
    return entry.models.filter((m) => m.source === "curated" && !onDisk.has(m.id));
  };

  const sections: MergedSection[] = groups.map((group) => ({
    key: group.key,
    label: group.label,
    note: group.note,
    disk: orderDisk(group.repos, loadedById),
    recommended: recommendedFor(group.key),
    size: group.size,
    runner: runnerOf(byCapability.get(group.key)),
  }));

  // Capabilities the DISK has never heard of. Appended in the catalog's order
  // and then sorted with everything else, which is what puts a recommended-only
  // "Image generation" between the two capabilities that do have models rather
  // than at the end.
  const seen = new Set(sections.map((s) => s.key));
  for (const entry of byCapability.values()) {
    if (seen.has(entry.capability)) continue;
    seen.add(entry.capability);
    const recommended = recommendedFor(entry.capability);
    if (!recommended.length) continue;
    sections.push({
      key: entry.capability,
      // Through `capabilityLabel` like every other heading on the page, so a
      // capability is put into words in exactly one place.
      label: capabilityLabel(entry.capability),
      note: null,
      disk: [],
      recommended,
      size: 0,
      runner: runnerOf(entry),
    });
  }

  return (
    sections
      // A section with neither half is not a section. It cannot come from a
      // disk group (`groupRepos` holds no empty buckets) but it is the state a
      // capability the catalog knows and nobody has models for would be in, and
      // an empty heading is worse than a missing one.
      .filter((section) => section.disk.length > 0 || section.recommended.length > 0)
      // Stable, so two capabilities the order list does not name keep the order
      // they arrived in — the listing's for a disk group, the catalog's for one
      // that is only recommended.
      .sort((a, b) => rank(a.key) - rank(b.key))
  );
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
  if (repo.partial) {
    // Ahead of the ENGINE readings below, because it is the only refusal here
    // about the download rather than about the model: a half-fetched snapshot
    // has no engine and often no capability either, so left further down this
    // chain a cancelled download explained itself as "no local engine reads this
    // repo's weight format" — a verdict on a file set that is not all there yet.
    // Behind the two above, because a component and a dataset are what they are
    // whether their fetch finished or not (see `resumable`).
    //
    // The card does not draw Load in this state at all; this is what keeps the
    // refusal honest wherever else it is asked.
    return partialNote(repo);
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
