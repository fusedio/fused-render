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
// No `engineHueStyle` import any more: one hue per engine family was a signal
// for a reader SWEEPING a grid of tags, and there are no engine tags on these
// faces to sweep — the engine is a row in the (i) now, in the same grey as
// every other fact in there.
import { tabHref } from "@apps/ai_models/routes";
import { gateChrome } from "@apps/ai_models/lib/hubSearchView";
import {
  hubSizeLabel,
  hubSizeTitle,
  knownTotalSize,
  lookupTotalSize,
} from "@apps/ai_models/lib/hubSize";
import { InfoButton } from "./ModelInfo";
import { modelName, RecommendedMark } from "./RepoCard";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { DownloadGlyph, ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { type AiCatalogModel, type HubModel } from "@platform/lib/api";
import { timeAgo } from "@platform/lib/format";
import { type Job } from "@platform/lib/jobs";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";

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
  /** For the one card that has to know whether it is on screen (see
   *  `HubResultCard`'s lazy size). */
  cardRef?: React.Ref<HTMLDivElement>;
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
      ref={cardRef}
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
            download — a recommendation is the one card that is curated by
            construction, so hiding the mark until the weights land was the page
            saying least where it was surest. */}
        {marked && <RecommendedMark />}
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
function SwitchEngines({ runner }: { runner: SectionRunner | null }) {
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
            <Button
            type="button"
            variant="outline"
            size="xs"
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
            </Button>
          </>
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
  recommended,
  runner,
  disk,
  authenticated,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: HubModel;
  /** Whether the curation names this exact repo id. A search is the Hub's list,
   *  not ours, so most rows are not marked — but a search that turns up a model
   *  this app recommends should say so, or the page's own opinion depends on
   *  which surface you found the model through. */
  recommended: boolean;
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
              : /* Nothing of it here — the faded end of the same axis. This card
                   is the one place all three disk answers land side by side in a
                   single list, so it is also where the axis has to be readable:
                   a search for "qwen" returns models this machine has and models
                   it does not, and the surface is what separates them before a
                   single word is read. */
                " am-card-none")
      }
      style={arriving === null ? undefined : { "--am-part": `${arriving * 100}%` }}
      cardRef={card}
      name={{
        href: model.url,
        /* The MODEL half, like every other card on this page — the owner leads
           the caption line below rather than eating the head's first third. */
        text: modelName(model.id),
        title: `Open ${model.id} on the Hub`,
      }}
      slug={model.id}
      marked={recommended}
      info={<InfoButton name={model.id} rows={[engineRow(runner, true)]} />}
      badges={
        <>
          {disk.state === "downloaded" && !busy && (
            <span className="am-suggest-have" data-hint={`${model.id} is already on this machine`}>
              ✓ downloaded
            </span>
          )}
          {/* The gate, named, with the whole of what to do about it on hover.
              This is NOT the pill D313 deleted: that one announced a problem
              and left a Download button beside it that would 403. Here the gate
              decides the action too — see the footer. */}
          {gate && (
            <span className="am-card-gate" data-hint={gate.title}>
              {gate.pill}
            </span>
          )}
        </>
      }
      size={{ text: size ?? "—", title: hubSizeTitle(model, total) }}
      what={
        /* The one tag left on the face, and it is STATE rather than identity —
           the same reason `RepoCard` kept it (D424). A half-fetched snapshot is
           not a model an engine can read, and it is what makes Download mean
           "resume" instead of "fetch". The engine tag that used to be the other
           arm of this ternary is a row in the (i) now. */
        disk.state === "partial" ? (
          <span
            className="am-card-engine am-card-engine-partial"
            tabIndex={0}
            aria-label={`${PARTIAL_TAG} — Download picks this up from the bytes already here.`}
            data-hint={
              `${model.id} is a download that did not finish. Download picks it up from the ` +
              "bytes already here rather than starting over; the Local view's trash discards them."
            }
          >
            {PARTIAL_TAG}
          </span>
        ) : null
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
              <Button
                variant="ghost"
                size="xs"
                render={<a
                // The same URL the Local view's Explore builds — a raw "#" +
                // path drops the mode, so a middle-click would land on the
                // folder listing rather than the model card.
                href={urlForFsPath(disk.path, "?_mode=model_card")}
                data-hint={`Explore ${model.id} here — ${disk.path}`}
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
                />}
              >
                Explore
              </Button>
            )}
            {/* A gate this machine cannot open gets the way to open it instead
                of a button that cannot start. The link goes to the model's own
                Hub page, which is where both the licence and the access
                request live. */}
            {gate?.action && (
              <Button
                variant="outline"
                size="xs"
                render={
                  <a
                    href={model.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    data-hint={gate.title}
                  />
                }
              >
                {gate.action}
              </Button>
            )}
            {/* The reason that Download is dead, where it is — same amber verb
                the other two cards use. */}
            {!loadable && <SwitchEngines runner={runner} />}
            {/* Nothing at all while the walk has not answered: both the ✓ and
                the button would be a claim about a disk nobody has read yet.
                And nothing on a copy we already have — the ✓ above and the
                Explore beside it are what that state offers. */}
            {(disk.state === "absent" || disk.state === "partial") &&
              (!gate || gate.canDownload) && (
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  disabled={!loadable}
                  data-hint={
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
                  <DownloadGlyph />
                  Download
                </Button>
              )}
          </>
        )
      }
    />
  );
}
