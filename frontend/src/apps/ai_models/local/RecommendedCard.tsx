// The other card in the Local tab's row: a model this machine does NOT have,
// that the curation says is the one to get for this capability.
//
// **Same skeleton, same classes, one species.** It is `.cc-mdcard.am-card` with
// the same head/what/foot bones as `RepoCard` beside it, and that is the whole
// argument for the merged row: a reader sweeping a carousel should see one kind
// of thing at two stages of its life — here, or a download away — rather than
// two grids of differently-shaped cards that happen to be about models. What
// differs is only what a model that is not here CANNOT have: no Load (nothing to
// load), no delete, no revisions, no "used 4h ago".
//
// The download plumbing is the Discover tab's, prop for prop, and deliberately
// not a second implementation: `busy` is the three-way `downloading ∪ starting ∪
// settling` union its `cardState` computes, the progress comes from the same
// `ModelProgress` reading the same job row, and the ✕ is the download manager's
// own cancel. Every one of those three sets covers a different gap between the
// click and the walk that confirms it (see `cardState` in discover/), and
// dropping any one puts the Download button back on live work.
import { hubModelUrl } from "./hub";
import { type SectionRunner } from "@apps/ai_models/lib/aiModelGroups";
import { ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { type AiCatalogModel } from "@platform/lib/api";
import { isRunning, type Job } from "@platform/lib/jobs";

export function RecommendedCard({
  model,
  runner,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: AiCatalogModel;
  /** Which backend would load it here, from the section — the card's engine
   *  tag. Null when the catalog resolved none, which is the one case with no tag
   *  to draw. */
  runner: SectionRunner | null;
  /** A pull for this model is live: reported, just clicked, or settling. */
  busy: boolean;
  /** Its download-manager row, once the supervisor has filed one. */
  job: Job | undefined;
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  const size = model.size_gb === null ? "—" : `${model.size_gb} GB`;
  // The download manager's own rule, not a looser one: a running job its
  // reporter never marked cancellable gets no ✕ rather than a dead one, and a
  // cancel already asked for is not asked again.
  const cancellable =
    job && isRunning(job) && job.cancellable && !job.cancel_requested && !job.stalled ? job : null;
  return (
    <div
      className="cc-mdcard am-card am-reccard"
      /* The curation's "why this one", on the card rather than in it. It is a
         sentence per card and this row scrolls sideways: rendered visibly it
         would set every card's height from the longest note in the section, on
         cards whose whole job is to be sweepable. */
      title={model.note ?? undefined}
    >
      <div className="cc-mdcard-head">
        {/* The NAME goes to the HUB, the same rule every other card on this page
            follows — the licence, the model card and the discussions are there
            and none of them are here. It shows the curation's `label` rather
            than the repo id, which is the id's readable half; the id itself is
            in the footer, in mono, where a reader who needs to type it looks. */}
        <a
          className="cc-mdcard-name am-card-name"
          href={hubModelUrl(model.id)}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${model.id} on the Hugging Face Hub`}
        >
          {model.label}
        </a>
        {/* Same slot, same figure, one difference: this one is what the download
            WILL cost, and an unmeasured size is a dash rather than a guess —
            nobody plans a multi-GB fetch around a number somebody invented. */}
        <span
          className="am-card-size"
          title={
            model.size_gb === null
              ? "Nobody has recorded this one's download size yet."
              : `About ${model.size_gb} GB to download`
          }
        >
          {size}
        </span>
      </div>
      {/* The engine tag, in the row RepoCard puts it in and wearing the same
          two states: the accent outline when that backend can load here, the
          dashed warning and the registry's own reason when it cannot. A
          recommendation for a capability nothing can serve is still worth
          showing (hiding it leaves somebody hunting for a feature that never
          was) — it just has no Download below it. */}
      <div className="am-card-what">
        {runner?.shortLabel && (
          <span
            className={"am-card-engine" + (runner.available ? "" : " am-card-engine-off")}
            tabIndex={runner.available ? undefined : 0}
            aria-label={
              runner.available
                ? undefined
                : `${runner.shortLabel} — cannot be loaded here: ${runner.reason ?? "unavailable"}`
            }
            title={
              runner.available
                ? `Loads in the ${runner.shortLabel} engine — the backend chosen for this capability on the Engines tab.`
                : `This is a ${runner.shortLabel} model, and it cannot be loaded here: ${runner.reason ?? "unavailable"}.`
            }
          >
            {runner.shortLabel}
          </span>
        )}
      </div>
      {/* No `detail` override: the job says what it is actually doing
          ("Fetching weights…", "Preparing MLX…") and a fixed word here would
          paper over a venv build with "Downloading". */}
      {busy && <ModelProgress job={job} />}
      <div className="cc-mdcard-foot">
        <span className="cc-mdcard-meta cc-mono" title={model.id}>
          {model.id}
        </span>
        <span className="cc-mdcard-actions">
          {busy ? (
            cancellable && (
              <button
                type="button"
                className="cc-iconbtn"
                title={`Stop downloading ${model.id}`}
                aria-label={`Stop downloading ${model.id}`}
                onClick={() => onCancel(cancellable)}
              >
                ✕
              </button>
            )
          ) : (
            <button
              type="button"
              className="am-card-power"
              /* Offered only where it can end in a model that runs. The tag
                 above already says which backend and why not; a Download that
                 filled the disk with weights nothing here reads would be the
                 page contradicting its own card. */
              disabled={!runner?.available}
              title={
                runner?.available
                  ? `Download ${model.id}${model.size_gb === null ? "" : ` (~${model.size_gb} GB)`}`
                  : `${model.id} cannot be loaded here: ${runner?.reason ?? "no engine serves this capability on this machine"}.`
              }
              aria-label={
                runner?.available
                  ? `Download ${model.id}`
                  : `Download ${model.id} — unavailable: ${runner?.reason ?? "no engine serves this capability on this machine"}`
              }
              onClick={onDownload}
            >
              Download
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
