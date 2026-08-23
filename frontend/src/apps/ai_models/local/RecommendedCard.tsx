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
import { useEffect, useRef, useState, type ReactNode } from "react";
import { hubModelUrl } from "./hub";
import {
  PARTIAL_TAG,
  jobFraction,
  type ResultDisk,
  type SectionRunner,
} from "@apps/ai_models/lib/aiModelGroups";
import { engineHueStyle } from "@apps/ai_models/lib/engines";
import { gateChrome } from "@apps/ai_models/lib/hubSearchView";
import {
  hubSizeLabel,
  hubSizeTitle,
  knownTotalSize,
  lookupTotalSize,
} from "@apps/ai_models/lib/hubSize";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { type AiCatalogModel, type HubModel } from "@platform/lib/api";
import { timeAgo } from "@platform/lib/format";
import { type Job } from "@platform/lib/jobs";
import { navigate, urlForFsPath } from "@platform/lib/router";

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
  cardRef,
  name,
  badges,
  size,
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
   *  it. Rendered as a title rather than as text because these cards sit in a
   *  scrolling row: a sentence per card would set every card's height from the
   *  longest one in the section. */
  hoverNote?: string;
  /** For the one card that has to know whether it is on screen (see
   *  `HubResultCard`'s lazy size). */
  cardRef?: React.Ref<HTMLDivElement>;
  /** The name, which always goes to the HUB — the same rule every other card on
   *  this page follows, because the licence, the model card and the discussions
   *  are there and none of them are here. */
  name: { href: string; text: string; title: string };
  /** ✓ downloaded, a gate, whatever else the head has to state. */
  badges?: ReactNode;
  /** What it costs, in the slot every card on this page puts a figure in. */
  size: { text: string; title?: string };
  what?: ReactNode;
  progress?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div
      className={`cc-mdcard am-card ${variant}`}
      style={style}
      title={hoverNote}
      ref={cardRef}
    >
      <div className="cc-mdcard-head">
        <a
          className="cc-mdcard-name am-card-name"
          href={name.href}
          target="_blank"
          rel="noopener noreferrer"
          title={name.title}
        >
          {name.text}
        </a>
        {badges}
        <span className="am-card-size" title={size.title}>
          {size.text}
        </span>
      </div>
      <div className="am-card-what">{what}</div>
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
function EngineTag({
  runner,
  capabilityOnly = false,
}: {
  runner: SectionRunner | null;
  capabilityOnly?: boolean;
}) {
  if (!runner?.shortLabel) return null;
  return (
    <span
      className={
        "am-card-engine" + (runner.available ? " am-card-engine-family" : " am-card-engine-off")
      }
      /* Same hue table as the disk card's tag (D436), resolved from the SHORT
         label here because that is all a card for a model nobody has downloaded
         has — `engineHue` matches the family prefix inside it. One engine, one
         colour, whichever card the reader is looking at. */
      style={engineHueStyle(runner.shortLabel)}
      tabIndex={runner.available ? undefined : 0}
      aria-label={
        runner.available
          ? undefined
          : `${runner.shortLabel} — cannot be loaded here: ${runner.reason ?? "unavailable"}`
      }
      title={
        !runner.available
          ? `This is a ${runner.shortLabel} model, and it cannot be loaded here: ${runner.reason ?? "unavailable"}.`
          : capabilityOnly
            ? `This kind of model loads in the ${runner.shortLabel} engine here — the backend chosen for the capability on the Engines tab. Whether this repo ships the weight format that engine reads is settled when the download lands.`
            : `Loads in the ${runner.shortLabel} engine — the backend chosen for this capability on the Engines tab.`
      }
    >
      {runner.shortLabel}
    </span>
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
         (a venv build), where an invented boundary would read as stalled. */
      variant={"am-reccard" + (arriving === null ? "" : " am-card-arriving")}
      style={arriving === null ? undefined : { "--am-part": `${arriving * 100}%` }}
      /* The curation's "why this one", on the card rather than in it. */
      hoverNote={model.note ?? undefined}
      name={{
        href: hubModelUrl(model.id),
        /* The curation's `label` rather than the repo id, which is the id's
           readable half; the id itself is in the footer, in mono, where a
           reader who needs to type it looks. */
        text: model.label,
        title: `Open ${model.id} on the Hugging Face Hub`,
      }}
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
      /* A recommendation for a capability nothing can serve is still worth
         showing (hiding it leaves somebody hunting for a feature that never
         was) — it just has no Download below it. */
      what={<EngineTag runner={runner} />}
      /* No `detail` override: the job says what it is actually doing
         ("Fetching weights…", "Preparing MLX…") and a fixed word here would
         paper over a venv build with "Downloading". */
      progress={busy && <ModelProgress job={job} />}
      meta={<span title={model.id}>{model.id}</span>}
      actions={
        busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
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
            Download
          </button>
        )
      }
    />
  );
}

function count(n: number | null): string | null {
  if (n === null || n === undefined) return null;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

/** One Hub search result: the same card, for a model the curation never named.
 *
 *  Three things it has that a recommendation does not, and each is why the
 *  search is worth having in the app rather than in a browser tab.
 *
 *  **The JOIN.** huggingface.co cannot tell you that the model you are reading
 *  about is already in your cache. This page can, and it asks its OWN listing
 *  rather than the `local` field on the search reply (`resultDisk`): that reply
 *  is frozen at the moment of the search, so a model downloaded from these very
 *  results would go on offering a Download button until somebody typed again.
 *  One definition of on-disk per page.
 *
 *  **The SIZE.** A cache fills up with multi-GB checkpoints nothing on screen
 *  mentions, so "≈16 GB" belongs next to a model's name before anyone decides to
 *  fetch it. When the search reply has no estimate — a GGUF or mflux repo
 *  publishes no dtype map — the card asks the Hub for the repo's total once it is
 *  on screen (`hubSize.ts`) rather than showing a dash for a number
 *  huggingface.co is perfectly willing to give.
 *
 *  **The GATE.** A licence you accept by signing in is a step the reader can
 *  take, so the card names the gate and offers the way through it rather than
 *  the search pretending the model is not there (D316).
 */
export function HubResultCard({
  model,
  runner,
  disk,
  authenticated,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: HubModel;
  /** Which backend would load this capability here, from the catalog — the same
   *  table the recommended cards read. */
  runner: SectionRunner | null;
  /** What this machine already has of it, from the page's own walk. */
  disk: ResultDisk;
  /** Whether this machine holds a Hub token. It belongs to the SEARCH, not to
   *  the model, which is why it arrives beside the row rather than in it. */
  authenticated: boolean;
  busy: boolean;
  job: Job | undefined;
  /** Starts — or, on a partial, RESUMES — the pull. */
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  // The FALLBACK total, for a repo the Hub's dtype map could not measure (see
  // `hubSize.ts`). Two constraints, and both are about not spending someone
  // else's rate limit: only a row with no estimate asks at all, and it waits
  // until this card is actually on screen. A page of two dozen results would
  // otherwise be two dozen outbound calls on every debounced keystroke.
  const card = useRef<HTMLDivElement>(null);
  const wantsTotal = !model.estimatedSize;
  // Seeded from the page-lifetime cache, so a card scrolled back to paints its
  // number immediately instead of flashing a dash.
  const [total, setTotal] = useState<number | null>(
    (wantsTotal ? knownTotalSize(model.id) : null) ?? null,
  );
  useEffect(() => {
    if (!wantsTotal) return;
    const known = knownTotalSize(model.id);
    if (known !== undefined) {
      setTotal(known);
      return;
    }
    const el = card.current;
    if (!el) return;
    let alive = true;
    // Asked, or asking: one request per visit to the viewport, so a card that
    // sits on screen while the answer arrives is not asked about twice. Cleared
    // when the card leaves view, which is what lets a FAILED ask be retried —
    // scroll away and back and the card tries again, rather than a single 429
    // costing this repo its size for the rest of the page's life. A card whose
    // ask succeeded stops observing entirely.
    let asking = false;
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) {
        asking = false;
        return;
      }
      if (asking) return;
      asking = true;
      lookupTotalSize(model.id).then((bytes) => {
        if (!alive) return;
        setTotal(bytes);
        // An answer — a number, or the Hub having none — is now cached and
        // cannot change while this page is open. Anything else was a failure
        // that nobody remembered, so keep watching for another chance.
        if (knownTotalSize(model.id) !== undefined) io.disconnect();
      });
    });
    io.observe(el);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [model.id, wantsTotal]);

  // The Hub's own measurement, and deliberately NOT replaced by a running
  // pull's own total the way a recommended card's is (`shared/modelSize`). The
  // size SORT ranks these cards by `hubSizeBytes`, which is defined to match
  // exactly what this cell shows — "the number beside a name is the only
  // evidence a reader has that a size sort worked" (`hubSize.ts`) — so a card
  // that swapped in a different figure mid-download would sit visibly out of
  // order in a size-sorted grid. One measurement per column beats a
  // more-accurate one on the row that happens to be busy.
  const size = hubSizeLabel(model, total);
  // What the Hub asks before it will hand this one over, when it asks anything.
  // Never on a copy we already hold: a gate is a condition on GETTING the model.
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const dl = count(model.downloads);
  const likes = count(model.likes);
  // The Hub sends an ISO timestamp; timeAgo works in epoch seconds. An
  // unparseable one is a field the card leaves out, not a "NaN ago".
  const updatedAt = model.updated ? Date.parse(model.updated) : NaN;
  const updated = Number.isFinite(updatedAt) ? timeAgo(updatedAt / 1000) : null;
  // A runner the catalog does not name is not a refusal here, unlike on a
  // recommended card: the server already dropped every row no registered runner
  // serves (D313), so a null runner means the catalog has not answered yet, and
  // disabling every Download until it does would break the page's one action for
  // a reason that is not about the model. A runner it names as UNAVAILABLE is a
  // refusal, for the recommended card's reason exactly.
  const loadable = !runner || runner.available;
  // Same wash as the recommended card and the disk card: a download in flight
  // fills the card it is filling.
  const arriving = jobFraction(job);

  return (
    <ModelCard
      /* The same two washes the Local view's cards wear, for the same two
         states (D436) — a search result IS a card about this disk once the
         model is on it, and two colour grammars for one fact would be two
         answers to "do I have this". No fraction on this side: the search reply
         says "partial" and nothing more, and this card has no folder to
         measure, so the wash is flat — which is the honest drawing of "some of
         it is here, we cannot say how much". */
      variant={
        "am-hubcard" +
        (arriving !== null
          ? " am-card-arriving"
          : disk.state === "downloaded"
            ? " am-card-have"
            : disk.state === "partial"
              ? " am-card-part am-card-part-unknown"
              : "")
      }
      style={arriving === null ? undefined : { "--am-part": `${arriving * 100}%` }}
      cardRef={card}
      name={{
        href: model.url,
        text: model.id,
        title: `Open ${model.id} on the Hub`,
      }}
      badges={
        <>
          {disk.state === "downloaded" && !busy && (
            <span className="am-suggest-have" title={`${model.id} is already on this machine`}>
              ✓ downloaded
            </span>
          )}
          {/* The gate, named, with the whole of what to do about it on hover.
              This is NOT the pill D313 deleted: that one announced a problem
              and left a Download button beside it that would 403. Here the gate
              decides the action too — see the footer. */}
          {gate && (
            <span className="am-card-gate" title={gate.title}>
              {gate.pill}
            </span>
          )}
        </>
      }
      size={{ text: size ?? "—", title: hubSizeTitle(model, total) }}
      what={
        disk.state === "partial" ? (
          /* The tag `RepoCard` wears for the same state, in the slot the engine
             tag would have used (D424): a half-fetched snapshot is not a model
             an engine can read, so naming one here would be a claim about a
             file set that is not all there yet. */
          <span
            className="am-card-engine am-card-engine-partial"
            tabIndex={0}
            aria-label={`${PARTIAL_TAG} — Download picks this up from the bytes already here.`}
            title={
              `${model.id} is a download that did not finish. Download picks it up from the ` +
              "bytes already here rather than starting over; the Local view's trash discards them."
            }
          >
            {PARTIAL_TAG}
          </span>
        ) : (
          <EngineTag runner={runner} capabilityOnly />
        )
      }
      progress={busy && <ModelProgress job={job} />}
      meta={
        <>
          {dl ? `${dl} downloads` : null}
          {dl && likes ? " · " : null}
          {likes ? `${likes} likes` : null}
          {(dl || likes) && updated ? " · " : null}
          {updated ? `updated ${updated}` : null}
        </>
      }
      actions={
        busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : (
          <>
            {/* The copy we already hold, opened where it lives. Only for a
                COMPLETE download: "partial" means blobs with no materialised
                snapshot, so there is no revision for the model card to
                describe and linking there would hand someone a view that
                cannot load. */}
            {disk.path && (
              <a
                className="am-card-explore-link"
                // The same URL the Local view's Explore builds — a raw "#" +
                // path drops the mode, so a middle-click would land on the
                // folder listing rather than the model card.
                href={urlForFsPath(disk.path, "?_mode=model_card")}
                title={`Explore ${model.id} here — ${disk.path}`}
                onClick={(e) => {
                  if (
                    e.defaultPrevented ||
                    e.button !== 0 ||
                    e.metaKey ||
                    e.ctrlKey ||
                    e.shiftKey ||
                    e.altKey
                  )
                    return;
                  e.preventDefault();
                  navigate(disk.path!, { isDir: true, mode: "model_card" });
                }}
              >
                Explore
              </a>
            )}
            {/* A gate this machine cannot open gets the way to open it instead
                of a button that cannot start. The link goes to the model's own
                Hub page, which is where both the licence and the access
                request live. */}
            {gate?.action && (
              <a
                className="am-card-power am-card-gate-link"
                href={model.url}
                target="_blank"
                rel="noopener noreferrer"
                title={gate.title}
              >
                {gate.action}
              </a>
            )}
            {/* Nothing at all while the walk has not answered: both the ✓ and
                the button would be a claim about a disk nobody has read yet.
                And nothing on a copy we already have — the ✓ above and the
                Explore beside it are what that state offers. */}
            {(disk.state === "absent" || disk.state === "partial") &&
              (!gate || gate.canDownload) && (
                <button
                  type="button"
                  className="am-card-power"
                  disabled={!loadable}
                  title={
                    !loadable
                      ? `${model.id} cannot be loaded here: ${runner?.reason ?? "unavailable"}.`
                      : disk.state === "partial"
                        ? `Resume downloading ${model.id} from the bytes already here`
                        : `Download ${model.id}${size ? ` (${size})` : ""}`
                  }
                  aria-label={
                    disk.state === "partial"
                      ? `Resume downloading ${model.id}`
                      : `Download ${model.id}`
                  }
                  onClick={onDownload}
                >
                  Download
                </button>
              )}
          </>
        )
      }
    />
  );
}
