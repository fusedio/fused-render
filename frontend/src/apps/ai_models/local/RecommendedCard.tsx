// The two cards on this page for a model that is not (yet) a repo on this disk:
// the curation's recommendation, and a Hub search result.
//
// **Same skeleton, same classes, one species.** Both are `.cc-mdcard.am-card`
// with the same head/what/foot bones as `RepoCard` beside them, and that is the
// whole argument for the merged page: a reader sweeping a carousel — or a grid
// of search results that replaced it — should see one kind of thing at several
// stages of its life rather than three grids of differently-shaped cards that
// happen to be about models. What differs is only what a model that is not here
// CANNOT have: no Load (nothing to load), no delete, no revisions, no "used 4h
// ago".
//
// That skeleton is `ModelCard` below, written once and composed twice, because
// the two cards were the place the page's card language would have drifted: the
// search results arrived from a tab of their own (D426) carrying their own copy
// of the head/what/foot markup, and two copies of a layout is two places for a
// class name to go stale.
//
// The download plumbing is one implementation for both, prop for prop: `busy` is
// the three-way `downloading ∪ starting ∪ settling` union the page computes, the
// progress comes from the same `ModelProgress` reading the same job row, and the
// ✕ is the download manager's own cancel. Every one of those three sets covers a
// different gap between the click and the walk that confirms it (see
// `spokenFor` in LocalTab), and dropping any one puts the Download button back
// on live work.
import { type ReactNode } from "react";
import { hubModelUrl } from "./hub";
import { jobFraction, type SectionRunner } from "@apps/ai_models/lib/aiModelGroups";
// No `engineHueStyle` import any more: one hue per engine family was a signal
// for a reader SWEEPING a grid of tags, and there are no engine tags on these
// faces to sweep — the engine is a row in the (i) now, in the same grey as
// every other fact in there.
import { tabHref } from "@apps/ai_models/routes";
import { InfoButton } from "./ModelInfo";
import { CuratedMark } from "./RepoCard";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { DownloadGlyph, ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { type AiCatalogModel } from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";
import { navigateUrl } from "@platform/lib/router";

/** The card every model on this page that is not a cache repo is drawn as.
 *
 *  Slots rather than props-per-fact: what this owns is the ORDER and the class
 *  names — head (name, badges, size), what, progress, foot (meta, actions) —
 *  which is exactly the part that must be identical between a recommendation
 *  and a search result. What goes IN each slot is the caller's, and it differs.
 */
function ModelCard({
  variant,
  style,
  hoverNote,
  name,
  marked,
  badges,
  size,
  slug,
  info,
  what,
  progress,
  meta,
  actions,
}: {
  /** The classes that differ — `.am-reccard` or `.am-hubcard`, plus whatever
   *  DISK state the caller is drawing (`am-card-have`, `am-card-part`), which is
   *  the same wash the Local view's own cards wear (D436). */
  variant: string;
  /** Inline custom properties for the classes above — today only `--am-part`,
   *  the fraction a download-in-flight wash is drawn to, which is a per-card
   *  NUMBER and so the one thing about these states a stylesheet cannot know. */
  style?: Record<string, string>;
  /** The card's own hover, where there is one thing to say about the whole of
   *  it. Rendered as a `data-hint` rather than as text because these cards sit
   *  in a scrolling row: a sentence per card would set every card's height
   *  from the longest one in the section. `data-hint`, not a native `title`,
   *  so it doesn't double up with a child control's own hint — the app's one
   *  tooltip system (`hints.ts`) walks up ancestors and lets the nearest
   *  `data-hint` win, where two native titles on the same point would both
   *  fire and show the browser's tooltip on top of the app's. */
  hoverNote?: string;
  /** The name, which always goes to the HUB — the same rule every other card on
   *  this page follows, because the licence, the model card and the discussions
   *  are there and none of them are here. */
  name: { href: string; text: string; title: string };
  /** ✓ downloaded, a gate, whatever else the head has to state. */
  badges?: ReactNode;
  /** What it costs, in the slot every card on this page puts a figure in — the
   *  CAPTION line under the name, ahead of the repo id. */
  size: { text: string; title?: string };
  /** The repo id, after the figure on that same line. */
  slug: string;
  /** The (i) in the head. Everything that is identity rather than state lives
   *  behind it, which on these cards is the engine tag. */
  info?: ReactNode;
  /** Whether the curation names this model — the seal after the name. Always
   *  true for a recommendation; asked per row for a Hub search result, which is
   *  a list the curation has no say over. */
  marked?: boolean;
  what?: ReactNode;
  progress?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div
      className={`cc-mdcard am-card ${variant}`}
      style={style}
      data-hint={hoverNote}
    >
      <div className="cc-mdcard-head">
        <a
          className="cc-mdcard-name am-card-name"
          href={name.href}
          target="_blank"
          rel="noopener noreferrer"
          data-hint={name.title}
        >
          {name.text}
        </a>
        {/* THE CURATION'S SEAL, on every card that earns it and not only the
            downloaded ones (Akshil, 2026-08-25: "do we show the badge on fused
            selected models that are downloaded only? I think we should show it
            on non-downloaded models as well"). It marks the MODEL, not the
            download — a recommendation card is a member of the shortlist by
            construction, so hiding the mark until the weights land was the page
            saying least where it was surest. */}
        {marked && <CuratedMark />}
        {badges}
        {info}
      </div>
      {/* THE CAPTION LINE, identical to the disk card's (2026-08-25). The figure
          used to sit in the head beside the name and the repo id down in the
          footer in mono, which made a recommendation a visibly different SHAPE
          from the cached card drawn next to it in the same row — "why are they
          not the same? at least the name, the mlx-community thing and the size
          should be same".
          Id first, figure pinned to the right edge (2026-08-27: "move the size
          all the way to the right in the card"). */}
      {/* THE HINT IS ON THE ID, not the line (Akshil, 2026-08-27: "this tooltip
          should only appear when I'm hovering on the ... model name text, not on
          the empty space between model and size"). The line spans the card, so
          a hint on it fired over the gap the figure's right-pin opened up. */}
      <div className="am-card-sub">
        <span className="am-card-slug cc-mono" data-hint={slug}>{slug}</span>
        <span className="am-card-size" data-hint={size.title}>
          {size.text}
        </span>
      </div>
      {what && <div className="am-card-what">{what}</div>}
      {progress}
      <div className="cc-mdcard-foot">
        <span className="cc-mdcard-meta cc-mono">{meta}</span>
        <span className="cc-mdcard-actions">{actions}</span>
      </div>
    </div>
  );
}

/** The engine tag, in the row `RepoCard` puts it in and wearing the same two
 *  states: the accent outline when that backend can load here, the dashed
 *  warning and the registry's own reason when it cannot.
 *
 *  One implementation for both cards, because "which engine loads this on this
 *  machine" is one question with one answer table (`runnersByCapability`) and a
 *  second copy of the tag is a second place for the two to disagree.
 *
 *  What the tag CLAIMS differs by card, though, and only in the hover. A curated
 *  model was picked FOR this runner, so "loads in MLX Whisper" is a fact about
 *  that repo. A search result only passed the server's TAG filter (HS-0: a
 *  registered runner serves this pipeline tag), and the tag says nothing about
 *  the weight format inside — a CTranslate2 Whisper repo is a speech-to-text
 *  result on a machine whose speech engine reads MLX. So `capabilityOnly` tells
 *  the hover to name the engine this CAPABILITY uses here, and to say that the
 *  repo's own format is settled when the files land: the alternative is the page
 *  promising a backend and then, one download later, drawing the same repo with
 *  `no engine` on it.
 */
function engineRow(runner: SectionRunner | null, capabilityOnly = false) {
  if (!runner?.shortLabel) return { label: "Engine", value: null };
  return {
    label: "Engine",
    value: runner.shortLabel,
    hint: !runner.available
      ? `This is a ${runner.shortLabel} model, and it cannot be loaded here: ${runner.reason ?? "unavailable"}.`
      : capabilityOnly
        ? `This kind of model loads in the ${runner.shortLabel} engine here — the backend chosen for the capability on the Engines tab. Whether this repo ships the weight format that engine reads is settled when the download lands.`
        : `Loads in the ${runner.shortLabel} engine — the backend chosen for this capability on the Engines tab.`,
  };
}

/** The way out of an engine that cannot serve this model here — the same amber
 *  `Switch engines` the disk card puts beside a dead Load.
 *
 *  It is what is left of `EngineTag`'s unavailable arm. The tag itself moved
 *  into the (i) (2026-08-25) because it is IDENTITY: which backend reads these
 *  weights is a fact about the model, read once, by someone who has stopped at
 *  one card. But the tag was carrying a second job in its `-off` state — the
 *  only thing on the card explaining why Download is greyed — and that half is
 *  not identity, it is the reason the control beside it does nothing. So it
 *  stays on the face, as the verb rather than the noun. */
export function SwitchEngines({ runner }: { runner: SectionRunner | null }) {
  const why = `${runner?.shortLabel ?? "This model"} cannot be loaded here: ${runner?.reason ?? "no engine serves this capability on this machine"}.`;
  return (
    <a
      className="am-card-fix"
      href={tabHref("engines", "")}
      data-hint={why}
      aria-label={`Switch engines — ${why}`}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        navigateUrl(tabHref("engines", ""));
      }}
    >
      Switch engines
    </a>
  );
}

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
  // One figure wherever this card names a size, and it never understates: the
  // catalog's approximate constant, or the running pull's own total once that
  // total is LARGER (see `shared/modelSize` for why larger and not merely
  // newer). A card showing both at once — `~64 GB` beside `68 GB / 68 GB` — read
  // as a download overrunning its own size.
  const size = modelSizeHint(model.size_gb, job);
  // How far the pull has got, or null when there is nothing to draw a boundary
  // at — no job yet, or a stage that reports no byte total.
  const arriving = jobFraction(job);
  return (
    <ModelCard
      /* The green wash tracks the download while it runs — the same drawing a
         partly downloaded repo card wears, and the reason this card needs it is
         the same: the bar in the progress row is 3px of a card the reader is
         watching from across a carousel. Nothing when the job reports no total
         (a venv build), where an invented boundary would read as stalled.

         Idle, it is `am-card-none`: a recommendation is by construction a model
         this machine does NOT have (the merge drops any the disk already
         answers for), so the faded end of the have/not-have axis is this card's
         resting state rather than a case it has to test for. */
      variant={"am-reccard" + (arriving === null ? " am-card-none" : " am-card-arriving")}
      style={arriving === null ? undefined : { "--am-part": `${arriving * 100}%` }}
      /* The curation's "why this one", on the card rather than in it. */
      hoverNote={model.note ?? undefined}
      name={{
        href: hubModelUrl(model.id),
        /* The curation's `label` rather than the repo id, which is the id's
           readable half; the id itself is on the caption line below, in mono,
           where a reader who needs to type it looks — and where the disk card
           keeps its own. */
        text: model.label,
        title: `Open ${model.id} on the Hugging Face Hub`,
      }}
      slug={model.id}
      marked
      info={<InfoButton name={model.id} rows={[engineRow(runner)]} />}
      size={{
        /* Same slot, same figure, one difference: this one is what the download
           WILL cost — the catalog's approximate constant, or the fetcher's own
           total once the pull reports one bigger than it, which is a number the
           progress row below is counting towards. An unmeasured size is a dash
           rather than a guess: nobody plans a multi-GB fetch around an invented
           number. */
        text: modelSizeLabel(model.size_gb, job),
        title:
          size === null
            ? "Nobody has recorded this one's download size yet."
            : size.approx
              ? `About ${size.text} to download`
              : `${size.text} — the size this download itself is reporting, which is more than the recorded estimate`,
      }}
      /* Nothing on the face any more: the engine tag it used to hold is a row
         in the (i) above. A recommendation for a capability nothing can serve is
         still worth showing (hiding it leaves somebody hunting for a feature
         that never was) — it just has `Switch engines` and a dead Download. */
      what={null}
      /* No `detail` override: the job says what it is actually doing
         ("Fetching weights…", "Preparing MLX…") and a fixed word here would
         paper over a venv build with "Downloading". */
      progress={busy && <ModelProgress job={job} />}
      /* The footer's left half is empty now — the repo id it held moved up to
         the caption line, where the disk card keeps its own. Nothing replaces
         it: a recommendation has no "used 4h ago" to state, because nothing on
         this machine has ever used it. */
      meta={null}
      actions={
        busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : (
          <>
            {!runner?.available && <SwitchEngines runner={runner} />}
            <button
            type="button"
            className="am-card-power"
            /* Offered only where it can end in a model that runs. The tag
               above already says which backend and why not; a Download that
               filled the disk with weights nothing here reads would be the
               page contradicting its own card. */
            disabled={!runner?.available}
            data-hint={
              runner?.available
                ? `Download ${model.id}${
                    size === null ? "" : ` (${size.approx ? "~" : ""}${size.text})`
                  }`
                : `${model.id} cannot be loaded here: ${runner?.reason ?? "no engine serves this capability on this machine"}.`
            }
            aria-label={
              runner?.available
                ? `Download ${model.id}`
                : `Download ${model.id} — unavailable: ${runner?.reason ?? "no engine serves this capability on this machine"}`
            }
              onClick={onDownload}
            >
              <DownloadGlyph />
              Download
            </button>
          </>
        )
      }
    />
  );
}

