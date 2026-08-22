// Hub addresses and commit shortening for a cached repo — the two derivations
// the Local tab's cards and its delete confirmations both need.
//
// Split out of the page for exactly that reason: `shortCommit` was called from
// the revision drawer, the delete banner and both modals, and `hubUrl` from the
// card head. Neither is state, neither is React.
import { type AiModelRepo } from "@platform/lib/api";


// Where a cached repo lives on the Hub. The cache folder encodes the KIND as
// well as the id, and the Hub's URL for a dataset or a Space is not the one for
// a model — `datasets--squad` is huggingface.co/datasets/squad, and linking it
// as huggingface.co/squad would be a 404 dressed up as a link.
const HUB_ORIGIN = "https://huggingface.co";
const HUB_PREFIX: Record<AiModelRepo["kind"], string> = {
  model: "",
  dataset: "datasets/",
  space: "spaces/",
};

export function hubUrl(repo: AiModelRepo): string {
  const id = repo.id.split("/").map(encodeURIComponent).join("/");
  return `${HUB_ORIGIN}/${HUB_PREFIX[repo.kind]}${id}`;
}

/** The same address for a model that is NOT on this disk — a recommended card,
 *  which has a repo id and none of the rest of an `AiModelRepo`.
 *
 *  Always the model prefix (the empty one): the curation recommends models, and
 *  a dataset or a Space is not something its Download button can pull. Here
 *  rather than as a second literal in `RecommendedCard` so the origin is written
 *  once — the two cards sit side by side in one row, and one of them linking to
 *  a stale mirror would be invisible.
 */
export function hubModelUrl(id: string): string {
  return `${HUB_ORIGIN}/${id.split("/").map(encodeURIComponent).join("/")}`;
}

export function shortCommit(commit: string): string {
  // Cache directories are named by full sha; the first 7 are what anyone reads.
  return /^[0-9a-f]{16,}$/i.test(commit) ? commit.slice(0, 7) : commit;
}