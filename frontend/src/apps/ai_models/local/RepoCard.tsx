// One cached repo, as a card: what it is, what it costs, what it is doing right
// now, and the three controls that act on it (Load/Unload, Try, Delete) — plus
// the (i) that opens everything else.
//
// Split out of the page file, where it and its three helper chips were ~570 of
// the Local tab's lines. Nothing about a card is page state: everything it
// draws arrives as a prop, and everything it does leaves as a callback, which
// is what let the page keep ONE call site for it (`card()` in LocalTab) across
// two differently-grouped sections.
//
// -- what the FACE is for (2026-08-24) ---------------------------------------
// The face states only what a reader gets by SWEEPING the grid: is this model
// here, is it loaded, is it arriving, what does it cost, and — when the answer
// is no — why it cannot be loaded. Everything that is IDENTITY rather than
// state (engine, parameters, quantization, weight format) moved behind the (i)
// in the head, because none of it is read except by somebody who has already
// stopped at one card. See `ModelInfo.tsx`.
//
// Gone with it: the task label, which repeated the section heading the card is
// filed under, and the revision drawer, which asked the reader to think in git
// commits about a folder whose only real question is how much disk it costs
// (Akshil: "does the user need to know this? if not, abstract it away"). A repo
// holding two commits reads as a bigger number and is deleted the same way.
import { ModelInfoButton } from "./ModelInfo";
import { hubUrl } from "./hub";
// No `CancelButton` import any more: this card's stop moved into the progress
// row on 2026-08-24 (see the headstone in the actions strip). The component is
// alive and still serves the recommended and search cards.
import { DownloadGlyph, ModelProgress } from "@apps/ai_models/shared/ModelProgress";
// `engineHueStyle` is gone with the engine tag: one hue per engine family was
// this card's loudest categorical signal (D436), and a hue exists to be picked
// out of a GRID. There is no tag in the grid to pick out any more — the (i)
// panel states the engine in the same grey as every other fact in it. The
// recommended and Hub search cards still wear the hue on their own tags.
import { unloadCountdown } from "@apps/ai_models/lib/engines";
import { type AiLoadedModel, type AiModelRepo } from "@platform/lib/api";
import { isRunning, type Job } from "@platform/lib/jobs";
import { formatSize, formatMtimeFull, timeAgo } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import {
  PARTIAL_TAG,
  emptyShell,
  jobFraction,
  loadRefusalShort,
  partialFraction,
  partialNote,
  resumable,
} from "@apps/ai_models/lib/aiModelGroups";
import { tabHref } from "@apps/ai_models/routes";
import { Trash2Icon } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Spinner } from "@platform/shadcn/ui/spinner";

/** Where the Try button goes: the playground, with this model selected and
 *  nothing else carried over. */
function tryHref(repo: AiModelRepo): string {
  return tabHref("playground", "?model=" + encodeURIComponent(repo.id));
}

/** The half of a repo id that names the MODEL, for the card's head.
 *
 *  A Hub id is `owner/name`, and the owner is the same string on every card in
 *  a section — six cards reading `mlx-community/…` spend their first third
 *  saying nothing that tells them apart, and the name itself then ellipsises.
 *  The owner is not lost: it leads the subtitle directly below, where the whole
 *  id is one muted line.
 *
 *  A bare id with no owner (`gpt2`, the Hub's legacy canonical models) is
 *  already the name, and is returned whole.
 */
export function modelName(id: string): string {
  const cut = id.lastIndexOf("/");
  return cut === -1 ? id : id.slice(cut + 1);
}

/** The curation's mark, at the end of the name.

 *  A VERIFIED BADGE — the scalloped seal with a check punched out of it, filled
 *  in accent (Akshil, 2026-08-25: "the tick mark does not pop that much, it
 *  looks like it is downloaded or something... you know the Meta Verified
 *  logo?"). A bare ✓ was the problem: a check beside a name is the universal
 *  mark for "done", and on a page whose cards are half about whether a download
 *  finished, it read as a second, quieter claim about the disk. The seal reads
 *  as an endorsement because that is the only thing the shape is ever used for.
 *
 *  Filled rather than stroked, for the same reason: at 13px a hairline check is
 *  a smudge, and a solid accent mark is the one thing on the card that pops
 *  without competing with the Loaded badge (which is filled GREEN, a different
 *  claim in a different hue).
 *
 *  Focusable, and hinted rather than `title`d: what it means is hover-only prose
 *  nothing else on the card repeats, and it must arrive instantly.
 */
export function RecommendedMark() {
  return (
    <span
      className="am-card-pick"
      tabIndex={0}
      data-hint="Recommended by Fused — one of the models this app suggests for its capability."
      aria-label="Recommended by Fused"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
        {/* The seal. Lucide's `badge-check` outline, filled instead of stroked. */}
        <path
          d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"
          fill="currentColor"
        />
        {/* …and the check knocked out of it in the card's own ground, so the
            mark reads as one solid object rather than two overlapping ones. */}
        <path
          d="m9 12 2 2 4-4"
          fill="none"
          stroke="var(--bg-alt)"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

// The loud one. A loaded model is the only state on this page that costs
// something continuously — gigabytes of RAM, right now — so it is the one state
// that has to be findable by SWEEPING a grid rather than by reading it. A 7px
// bullet was not: at a glance a loaded card looked exactly like the eleven
// cached cards around it. Green, filled, in the card's head, and the card
// itself changes colour underneath it (styles/ai-models.css). The same green as
// the sidebar's live dot, because "this is running" should be one colour in
// this app rather than one per surface.
function LoadedBadge({ loaded }: { loaded: AiLoadedModel }) {
  return (
    <span
      className="am-loaded-badge"
      data-hint={
        `${loaded.model} is loaded in memory` +
        (loaded.residentBytes ? ` — ${formatSize(loaded.residentBytes)} resident` : "")
      }
    >
      Loaded
    </span>
  );
}

// The live state of one model: what it is doing, and what it is costing.
//
// Four states worth distinguishing, and the distinctions are the point:
// downloading (bytes, from the job row — the only place byte counts exist),
// loading (no percentage, because weights going into memory is one opaque step
// and an invented bar reads as frozen), ready (with its resident memory), and
// error (with what went wrong, because "it failed" sends people nowhere).
/** Where a loaded model actually ended up, and — on a CPU — what that means.
 *
 *  **The CPU case is the one this exists for, and since the per-hardware runner
 *  split it is the DEFAULT case.** torch runs on whatever it can see, and the
 *  engine `auto` picks off Apple Silicon is the CPU-only build, so a perfectly
 *  healthy 4B model answers at a few words a second. Without this the
 *  page shows a green LOADED card and a memory figure, both of which say the
 *  model is fine, and leaves the user to conclude from the speed that it is not.
 *  A GPU is reported too — quietly, as a fact — because a chip that appears only
 *  when something is slow is a warning, and this is information.
 */
function DeviceNote({ device }: { device: string }) {
  const cpu = device === "cpu";
  const label = cpu ? "on CPU" : `on ${device.toUpperCase()}`;
  return (
    <span
      className={"am-runtime-device" + (cpu ? " am-runtime-device-cpu" : "")}
      data-hint={
        cpu
          ? "This model is running on the processor, not a graphics card — it " +
            "works, but expect a few words a second rather than an instant " +
            "answer. The CPU engine is the default off Apple Silicon — if this " +
            "machine has a supported graphics card, a CUDA or ROCm engine can " +
            "be chosen on the Engines tab."
          : "This model is running on the graphics card."
      }
    >
      {label}
      {cpu && <span className="am-runtime-device-tail"> — a few words a second</span>}
    </span>
  );
}

function RuntimeChip({
  loaded,
  job,
  stop,
}: {
  loaded?: AiLoadedModel;
  job?: Job;
  /** Passed straight through to `ModelProgress` — see the note there on why the
   *  cancel lives in the progress row and not in the actions strip. Only the
   *  BUSY arm below takes it: a ready model's row is a memory figure, and a
   *  failed one's is an error, and neither is work anybody can stop. */
  stop?: { label: string; onStop: () => void };
}) {
  if (loaded?.state === "ready") {
    // The badge above already said "loaded"; this row carries the two things a
    // badge cannot — the number, and the device. Nothing at all when the worker
    // could answer with neither, rather than a row that repeats the badge.
    //
    // Both are optional and independently so: a runner that cannot measure its
    // own memory still knows where it put the weights, and an early return on
    // the memory figure alone would have thrown the device away with it.
    if (!loaded.residentBytes && !loaded.device) return null;
    // AI-13: null whenever the idle window is disabled — by the stored pref,
    // by an env override, or (rarely) by neither on a machine with no reaper
    // running — and the page has no reason to tell those apart, since none
    // of them counts down.
    const countdown = unloadCountdown(loaded.unloadsInSeconds);
    return (
      <div className="am-card-runtime am-card-runtime-ready">
        {loaded.residentBytes ? (
          <span
            className="am-runtime-mem am-runtime-mem-lead"
            data-hint={
              "Resident memory of the model's process. Not the model's size: it " +
              "counts shared pages too and moves while it generates."
            }
          >
            {formatSize(loaded.residentBytes)} in memory
            {countdown ? ` — ${countdown}` : ""}
          </span>
        ) : null}
        {loaded.device && <DeviceNote device={loaded.device} />}
      </div>
    );
  }
  if (loaded?.state === "error") {
    return (
      <div className="am-card-runtime am-card-runtime-error" data-hint={loaded.error ?? undefined}>
        Failed to load{loaded.error ? ` — ${loaded.error}` : ""}
      </div>
    );
  }
  return <ModelProgress detail={loaded?.detail} job={job} stop={stop} />;
}

export function RepoCard({
  repo,
  label,
  recommended,
  loaded,
  job,
  busy,
  fetching,
  refusal,
  resumeCapability,
  estimate,
  onDeleteRepo,
  onDownload,
  onCancel,
  onLoad,
  onUnload,
}: {
  repo: AiModelRepo;
  /** The curated display name for the card's HEAD (AI-2c, catalog.py): a
   *  human name is a curated field, never one mechanically derived from a
   *  repo id at runtime. Looked up by the page (`LocalTab.tsx`'s
   *  `labelByRepoId`) from `AiCatalogModel.label` — the unique, size/quant-
   *  carrying field, not `nickname`, which deliberately collides within a
   *  model family. `undefined` for a repo the curation does not name (most
   *  engine-fetched repos, and anything outside the shortlist), in which
   *  case the head falls back to `modelName(repo.id)` — the mechanical
   *  strip this prop exists to avoid wherever a curated name IS available.
   *  Every other identifier on the card (Load/Delete/Try hints, the subtitle
   *  slug) keeps using `repo.id` — this prop only ever touches the head. */
  label?: string;
  /** Whether the curation names this exact repo id — the tick beside the name.
   *  Decided by the page, which holds the catalog; a repo row itself has no
   *  opinion about whether we recommend it. */
  recommended: boolean;
  /** The resident worker for this repo, when it is one. */
  loaded: AiLoadedModel | undefined;
  /** Its download-manager row, while a bring-up is running. */
  job: Job | undefined;
  busy: boolean;
  /** True while a weights-only fetch for this repo is in flight. */
  fetching: boolean;
  /** Why Load is refused for this repo, or null when it can be loaded
   *  (`aiModelGroups.loadRefusal`). It DISABLES the button and becomes its
   *  title; it never removes it. A model that is already RESIDENT must still be
   *  releasable whatever this says — see the render below. */
  refusal: string | null;
  /** For a `repo.partial` card: which capability a RESUME would be filed under,
   *  or null when nothing on this page can say. A half-fetched snapshot's own
   *  `capability` is read off the files that happened to land, so the curation
   *  answers for every model this app recommends. A repo NEITHER can place has
   *  no resume to offer, and the card swaps its primary control for the one act
   *  that works on it — see the Delete branch in the footer (D437). */
  resumeCapability: string | null;
  /** What the CURATION says this model's whole download weighs, in bytes, or
   *  null for a repo it never named. Only a partly downloaded card reads it, as
   *  the denominator of the fraction it paints (`partialFraction`) — and only
   *  when no live job is reporting real byte counts, which is the better
   *  answer. */
  estimate: number | null;
  onDeleteRepo: () => void;
  /** Resume the unfinished download. The server picks up from the bytes on disk
   *  (D275) — this is the same POST the recommended card's Download sends. */
  onDownload: () => void;
  /** Stop the pull that is running for this repo. The download manager's own
   *  cancel, the same one the recommended and search cards send. */
  onCancel: (job: Job) => void;
  onLoad: () => void;
  onUnload: () => void;
}) {
  const when = timeAgo(repo.lastUsed ?? repo.mtime);
  const live = loaded?.state === "ready";
  // WEIGHTS ARE COMING INTO MEMORY RIGHT NOW — not resident, not failed, not a
  // disk fetch. `starting`, `venv`, `downloading` and `loading` all land here
  // (supervisor.Worker.state), because from this card's side they are one fact:
  // a load has been asked for and has not answered yet.
  const loading = !!loaded && !live && loaded.state !== "error";
  // Why Delete is not offered right now, or "". Deleting files a worker is
  // reading or holding open corrupts a load in progress, and on a RESIDENT
  // model it is quieter and worse — the weights are already mapped, so the
  // delete appears to work and the model answers on until something unloads it.
  // The server refuses these too (`_require_not_in_use`); this is so the answer
  // arrives before the confirm dialog rather than after it.
  const inUse = live
    ? "in memory — unload it first"
    : loaded
      ? `being loaded (${loaded.state})`
      : fetching
        ? "being downloaded"
        : "";
  // -- what the card's own SURFACE says (D436) ------------------------------
  // Three of the four states this page has are facts about the disk, and until
  // now the card's background stated exactly one of them (loaded). The two it
  // did not are the two a reader most often wants by sweeping: which of these
  // do I already have, and which one stopped halfway.
  // While a pull is RUNNING the wash is green and tracks the job; idle, it is
  // amber and reports the disk. Same boundary, two meanings — "arriving" and
  // "stopped" — which is the distinction the tag beside it cannot draw, since a
  // partial repo wears `partly downloaded` in both states.
  const pulling = jobFraction(job) !== null;
  const part = pulling || resumable(repo) ? partialFraction(repo, job, estimate) : null;
  // "Complete, on this disk, and not resident." The card's WASH and nothing else
  // now: the footer chip that also said it was removed by request (D448) — the
  // green surface had made it a second answer to a question already answered, and
  // in a row where most cards are downloaded it was a badge on almost every one.
  // NOT gated on `loaded` — a model
  // whose weights are going INTO memory is still a model this machine has, and
  // dropping the wash for the seconds a load takes would flash the one card the
  // reader is watching. It ends when `am-card-loaded` takes over, which says
  // more.
  const have = !live && !resumable(repo);
  // A partial repo with an unknown denominator gets a flat wash instead of a
  // fraction: some of it is here and nothing on this page can say how much,
  // which is a different sentence from "2% of it is here".
  const partClass = pulling
    ? " am-card-arriving"
    : !resumable(repo)
      ? ""
      : part === null
        ? " am-card-part am-card-part-unknown"
        : " am-card-part";
  // -- the one way out of work in flight (2026-08-24) -------------------------
  // Both kinds of busy get a stop, and they are DIFFERENT calls to different
  // things, which is the whole reason this is decided here rather than inside
  // the progress row: a download is a job the manager owns and cancels, while a
  // load is a worker process, and the thing that stops one of those is `unload`.
  //
  // A DOWNLOAD FIRST, when there is one, because a load that is currently
  // pulling weights is reporting the pull — the bytes on the row are the job's —
  // and the button beside them has to stop the work the row is describing.
  // `cancelJob`'s own eligibility rule is kept verbatim (it was CancelButton's,
  // see the headstone in the actions strip below): a job its reporter never
  // marked cancellable gets no control rather than a dead one, and a cancel
  // already asked for is not asked twice.
  //
  // `supervisor.unload()` really does stop a loading worker — it filters by
  // model with no state check and terminates what it claims. The `state ===
  // "ready"` rule that reads like a bar on this is the IDLE REAPER's predicate
  // (`_is_idle`), which is a different question: whether to unload a worker
  // nobody asked about. Asked directly, mid-load, the answer is yes.
  const stoppableJob =
    job && isRunning(job) && job.cancellable && !job.cancel_requested && !job.stalled ? job : null;
  const stop = stoppableJob
    ? { label: `Stop downloading ${repo.id}`, onStop: () => onCancel(stoppableJob) }
    : loading
      ? { label: `Stop loading ${repo.id}`, onStop: onUnload }
      : undefined;
  return (
    <div
      className={
        "cc-mdcard am-card" + (live ? " am-card-loaded" : have ? " am-card-have" : "") + partClass
      }
      /* The fraction as a custom property, read by a hard-stop gradient in
         ai-models.css. Inline because it is a per-card NUMBER — the only thing
         about these states a stylesheet cannot know — and it is a background
         only: no size, no border, no radius, so a card changing state never
         reflows the carousel row it sits in. */
      style={part === null ? undefined : { ["--am-part" as string]: `${part * 100}%` }}
    >
      <div className="cc-mdcard-head">
        {/* The NAME goes to the HUB. A repo id is a Hub address, and the page
            it names is where the licence, the full model card and the
            discussions live — none of which this machine has. Opening the copy
            we DO have is a different act with a different destination, so it
            gets its own control (Know more, in the (i) panel) instead of
            competing for the same click. Still a real <a href>, so middle-click
            and copy-link behave — and its href is the whole id even though only
            the model half is drawn. */}
        <a
          className="cc-mdcard-name am-card-name"
          href={hubUrl(repo)}
          target="_blank"
          rel="noopener noreferrer"
          data-hint={`Open ${repo.id} on the Hugging Face Hub`}
        >
          {label ?? modelName(repo.id)}
        </a>
        {recommended && <RecommendedMark />}
        {loaded?.state === "ready" && <LoadedBadge loaded={loaded} />}
        {/* Only when the kind is NOT the one the page already promises. A page
            titled "AI Models" listing eight cards each tagged MODEL states the
            obvious eight times and spends head-row width doing it. A dataset or
            a Space in the same cache is the exception the reader has to notice —
            it is not loadable, its Hub address is a different one (HUB_PREFIX),
            and the tag is the only thing on the card that says so. */}
        {repo.kind !== "model" && <Badge variant="outline">{repo.kind}</Badge>}
        {/* Everything the face no longer carries, one click away and costing
            the head one 22px button. Last in the row, so it sits in the card's
            top-right corner whatever the name's length. */}
        <ModelInfoButton repo={repo} />
      </div>
      {/* WHICH REPO IT IS, THEN WHAT IT COSTS — one line, directly under the
          name (Akshil, 2026-08-25: "the size should not be after the model name
          checkmark, it should be right below it"; 2026-08-27: "move the size
          all the way to the right in the card"). The id continues the name
          above it, and the figure sits pinned to the card's right edge, where
          a column of cards lines its figures up against a shared margin.

          No separator between them: a middot in `--border` at 11px on a card
          wash is a mark nobody can see ("the dot here is barely visible, let's
          just remove it"), and the gap already does the work.

          Ellipsised on the id, which is the half that can be arbitrarily long,
          with the whole thing on hover — a wrapped second line would set every
          card's height from the longest repo name in the section. */}
      {/* THE HINT IS ON THE ID, not the line (Akshil, 2026-08-27: "this tooltip
          should only appear when I'm hovering on the ... model name text, not on
          the empty space between model and size"). The line spans the card, so
          a hint on it fired over the gap the figure's right-pin opened up. */}
      <div className="am-card-sub">
        <span className="am-card-slug cc-mono" data-hint={repo.id}>{repo.id}</span>
        <span
          className="am-card-size"
          data-hint={repo.mtime ? `Last changed ${formatMtimeFull(repo.mtime)}` : undefined}
        >
          {formatSize(repo.size)}
        </span>
      </div>
      {/* THE TWO TAGS THAT ARE STATE, not identity — and nothing else.
          This row held five chips (engine, task, parameters, quantization,
          weight format); all five are now rows in the (i) panel, because each
          one is read by somebody deciding about ONE model rather than by a
          reader sweeping the grid. The task label went entirely: it repeated
          the section heading the card is filed under.

          What survives are the two tags that explain the BUTTON below them, and
          they are worth a glance across a whole grid:

          - `part of X` — why this card has no Load at all. A repo the user
            never downloaded on purpose: the 2.4GB GGUF is FLUX's transformer
            and deleting it breaks that model, the 2MB Silero is the whisper
            engine's speech detector and deleting it only costs speed. The hover
            carries what deleting THIS one costs, which differs per component
            and is prose nothing else repeats — hence the tab stop.

          - `partly downloaded` — why the button reads "Continue downloading".
            It OUTRANKS every reading of the engine (D424): both were drawn from
            the same half-fetched snapshot, and a cancelled MLX Whisper pull has
            no weights yet, so the engine's verdict on it is a claim about a file
            set that is not all there.

          The row is drawn only when one of them applies. It used to render
          unconditionally so every card had the same number of rows; the
          subtitle above now does that job on every card, including the
          metadata-less ones. */}
      {(repo.component || resumable(repo)) && (
        <div className="am-card-what">
          {repo.component ? (
            <span
              className="am-card-engine am-card-engine-component"
              tabIndex={0}
              aria-label={`Part of ${repo.component.owner} — ${repo.component.what}`}
              data-hint={repo.component.what}
            >
              part of {repo.component.owner}
            </span>
          ) : (
            <span
              className="am-card-engine am-card-engine-partial"
              tabIndex={0}
              aria-label={`${PARTIAL_TAG} — ${partialNote(repo)}`}
              data-hint={partialNote(repo)}
            >
              {PARTIAL_TAG}
            </span>
          )}
        </div>
      )}
      {/* What this model is doing RIGHT NOW, as opposed to what it is. Absent
          when the answer is "sitting on disk", which is what every card would
          otherwise say — a row of identical chips carries no information. */}
      {(loaded || job) && <RuntimeChip loaded={loaded} job={job} stop={stop} />}
      <div className="cc-mdcard-foot">
        {/* ONE fact, not five. "15 files · main · used 4h ago · added 4h ago"
            was four numbers competing for the same glance, and only one of
            them is ever the reason someone is looking at this grid: how long
            it has been since anything read this. `added` survives as a row in
            the (i) panel; the file count and the branch were dropped outright
            (Akshil, 2026-08-24: "i don't think we need these two things") —
            neither answers a question anyone brings to this page, and the
            branch is a git fact about a folder nobody browses as a repo. */}
        <span className="cc-mdcard-meta">{when ? `used ${when}` : ""}</span>
        <span className="cc-mdcard-actions">
          {/* Load / Unload — the one control on this page that costs MEMORY
              rather than disk.

              **Always rendered, disabled when it cannot be pressed.** It used
              to disappear for a repo no engine here can load, and a control
              that vanishes teaches nothing: comparing two cards, a user cannot
              tell "this model cannot be loaded" from "I misremembered where the
              button was", and the row's width shifted card to card so the eye
              never learned where to look. `refusal` carries the reason — and
              there are four different ones, which is the other half of the
              argument: a disabled button with no explanation is the same dead
              end as a missing one. */}
          {/* `loaded` FIRST, and the refusal only for the Load half. Residency
              is a FACT the runtime reported; the refusal rests on an INFERENCE
              from model-card metadata and the format on disk, and the two can
              disagree — FLUX.2 klein's card says "image to image", which no
              runner serves, while the model is sitting in memory loaded as
              text-to-image. Gating both halves on the inference stranded it: the
              card said Loaded and offered no way to get the memory back. What is
              resident can always be unloaded. */}
          {/* RESIDENT only, since 2026-08-24 — it was `loaded ?`, which is any
              worker at all including one still starting up. Two things were
              wrong with offering Unload mid-load. It is the wrong VERB: nothing
              is loaded yet, so the reader is being offered to undo a state the
              card is not in. And it displaced the control that state does need
              — the way to stop the load — which then had to be squeezed in
              beside it as a ✕ that shifted the strip every time a job came and
              went (see the headstone below). Mid-load the button reads
              `Loading…` and is disabled, exactly like the download arm's own
              in-flight label, and the stop lives on the progress row where the
              bar is. */}
          {/* WHY THE LOAD BESIDE IT IS DEAD — in the strip, not under it.
              It was a `<p>` below the actions, and at card width the sentence
              wrapped to two or three lines and became the largest block of ink
              on the card (Akshil: "reduce this message more, and it should be
              next to load button").

              TWO SHAPES, decided by whether there is anything to DO about it
              (2026-08-25).

              When the obstacle is which engine this capability is pointed at,
              the whole thing is one warning-coloured link reading `Switch
              engines` — two words, an amber that earns a glance beside a greyed
              button, and a press that lands on the tab where the setting lives.
              It replaced `Set to MLX FLUX` + a separate `Engines` link, which
              was a statement of configuration ("i don't know what it means")
              followed by a noun. What a reader wants there is the verb.

              Everything else — a component, a dataset, a format nothing here
              reads — has no destination that would help, so it stays a muted
              phrase that ellipsises into whatever width the buttons leave.

              Both carry the FULL registry sentence as `data-hint`, which is the
              app's instant tooltip (platform/lib/hints) rather than a native
              `title`: a `title` waits out the browser's own delay, and on a
              window's first hover that is seconds. The disabled button's
              `aria-label` still carries the same prose for a screen reader.

              Same gate as the paragraph it replaces: only the arms that actually
              draw a DISABLED Load. A partly-downloaded repo's `refusal` explains
              a button that is not there — its control is an enabled "Continue
              downloading", and the tag above already says why. */}
          {refusal && !live && !loading && !resumable(repo) && (
            repo.engine && !repo.engine.available ? (
              /* Decided STRUCTURALLY, on the engine's own `available` flag, and
                 never by reading words in the reason: that prose comes from the
                 registry and matching it would silently stop working the day it
                 is reworded. */
              <a
                className="am-card-fix"
                href={tabHref("engines", "")}
                data-hint={refusal}
                aria-label={`Switch engines — ${refusal}`}
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
                  navigateUrl(tabHref("engines", ""));
                }}
              >
                Switch engines
              </a>
            ) : repo.component ? (
              /* NOTHING, on a component card. The `part of FLUX.2 klein 4B` tag
                 two lines up says exactly this, and the strip was repeating it
                 word for word beside the button — "Part of FLUX.2 klein 4B  [Load]"
                 under a tag reading `part of FLUX.2 klein 4B` (Akshil,
                 2026-08-25). The tag is the better copy of the two: it carries
                 the hover that says what deleting this one actually costs, which
                 differs per component. The disabled Load keeps the full sentence
                 in its own `aria-label`. */
              null
            ) : (
              <span className="am-card-why" data-hint={refusal}>
                {loadRefusalShort(repo) ?? refusal}
              </span>
            )
          )}
          {live ? (
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled={busy}
              data-hint={`Unload ${repo.id} and give its memory back`}
              onClick={onUnload}
            >
              Unload
            </Button>
          ) : loading ? (
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled
              data-hint={`${repo.id} is loading into memory — the ✕ on the progress row stops it`}
              aria-label={`${repo.id} is loading`}
            >
              <Spinner data-icon="inline-start" />
              Loading…
            </Button>
          ) : resumable(repo) && !resumeCapability ? (
            /* THE DEAD END, given a way out (D437). A user hit this in the
               wild: a folder holding one 40-byte `refs/main` and nothing else,
               filed under Unrecognised, tagged "partly downloaded", with a
               DISABLED Download beside it whose hover said to delete it. Every
               word of that was true and the card still had no working control on
               it — the trash was an unlabelled glyph third in a row of four, and
               the user went to Finder and deleted the folder by hand.
               So when the resume is impossible, the primary control becomes the
               act that IS possible. Same trash target, same confirm dialog: this
               is the labelled door to it, not a second way of deleting. */
            <Button
              type="button"
              variant="destructive"
              size="xs"
              disabled={busy || !!inUse}
              data-hint={
                inUse
                  ? `Cannot delete ${repo.id}: ${inUse}`
                  : `Delete ${repo.id} — ${
                      emptyShell(repo)
                        ? "this download stopped before any of the model arrived, so there is nothing to resume"
                        : "Fused Render cannot tell what this is, so the download cannot be resumed"
                    }`
              }
              aria-label={`Delete ${repo.id} — this unfinished download cannot be resumed`}
              onClick={onDeleteRepo}
            >
              Delete
            </Button>
          ) : resumable(repo) ? (
            /* The one card state whose control is a DOWNLOAD rather than a Load
               (D424). There is nothing to load — the snapshot is incomplete —
               and the fetch is resumable, so the honest offer is the rest of it:
               the server picks up from the part file on disk instead of starting
               over. **"Continue downloading", not "Download"** (D448): the same
               class as the recommended card's button because it is the same act,
               but not the same WORD, because it is not the same act at the same
               stage — "Download" on a card that already holds two thirds of the
               model reads as an offer to start over, which is the one thing this
               button does not do. With the trash beside it, those are the two
               ways out of this state.
               Disabled while the pull is actually running, where "Downloading…"
               is what the label says and the job row below carries the bytes. */
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled={busy || fetching || !!job}
              data-hint={`Continue downloading ${repo.id} — it resumes from the ${formatSize(repo.fetchedBytes)} already here`}
              aria-label={`Continue downloading ${repo.id} — resume the unfinished download`}
              onClick={onDownload}
            >
              {fetching || job ? <Spinner data-icon="inline-start" /> : <DownloadGlyph />}
              {fetching || job ? "Downloading…" : "Continue downloading"}
            </Button>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled={busy || !!job || !!refusal}
              data-hint={refusal ?? `Load ${repo.id} into memory so it can answer`}
              /* The reason again, in the accessible name. A hover-only hint is
                 one a pointer user may never think to hover — while a screen
                 reader reads the name and nothing else. It opens with the
                 visible label so the name still contains what is on screen
                 (WCAG 2.5.3). */
              aria-label={
                refusal ? `Load ${repo.id} — unavailable: ${refusal}` : `Load ${repo.id}`
              }
              onClick={onLoad}
            >
              {job && <Spinner data-icon="inline-start" />}
              {job ? "Loading…" : "Load"}
            </Button>
          )}
          {/* THERE IS NO CancelButton HERE ANY MORE (2026-08-24). It was the way
              out of a running download (D440) — necessary, because while a pull
              is live the button above is disabled and the trash is disabled too
              (`inUse`), so without it a 40GB fetch started by mistake had to be
              cancelled from the download manager or waited out.
              Nothing about that need changed; only where the control sits. In
              this strip it was rendered conditionally, so it grew and shrank the
              row on every job transition — a 26px target that MOVES while being
              aimed at, next to a button whose label was `Unload`. Akshil, on
              trying to cancel a load: "I don't notice it and I cannot click it
              because it is fast." It is now the trailing control of the progress
              row (`stop`, above, and ModelProgress's own note), which is drawn
              only while there is work and therefore costs no layout when there
              is none. `CancelButton` itself is untouched and still serves the
              recommended and search cards, whose actions row has no in-flight
              label to collide with. */}
          {/* Into the Playground, pre-selected — the tab whose whole job is
              "use it now". Only where the playground could actually serve it:
              the same loadability verdict the Load button rests on, since a
              model the resolved engine refuses would land on a sidebar that
              silently falls back to a different model. A real <a href>, like
              Explore beside it. */}
          {repo.capability && !refusal && (
            <Button
              variant="ghost"
              size="xs"
              className="am-card-try"
              render={<a
              /* An EXPLICIT search, not the current one: `tabHref`'s default is
                 to carry the query across a tab switch (see routes.ts), which
                 is right for the strip and wrong here — this link's whole job
                 is to REPLACE whatever model the playground had selected with
                 this card's. Anything else in the query is a stage setting
                 dialled in for a different model. */
              href={tryHref(repo)}
              data-hint={`Try ${repo.id} in the Playground`}
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
                navigateUrl(tryHref(repo));
              }}
              />}
            >
              Try
            </Button>
          )}
          {/* NO EXPLORE ICON, AND NO CHEVRON (2026-08-24). Explore is the same
              destination it always was — this folder's own model card view
              (SPEC §38) — but as a NAMED control ("Know more") in the (i) panel
              rather than a third unlabelled glyph in a row of four. The chevron
              had nothing left to open once the drawer's facts became rows in
              that panel. What is left in this strip is one verb, one link and
              one trash: the acts, and nothing that merely reveals. */}
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="text-destructive"
            data-hint={inUse ? `Cannot delete ${repo.id}: ${inUse}` : `Delete ${repo.id}`}
            aria-label={`Delete ${repo.id}`}
            disabled={!!inUse}
            onClick={onDeleteRepo}
          >
            <Trash2Icon />
          </Button>
        </span>
      </div>
      {/* NO REFUSAL PARAGRAPH, AND NO DRAWER, BELOW THIS POINT (2026-08-24).

          The paragraph that explained a dead Load moved INTO the actions strip
          as `am-card-why` — same gate, same short sentence, same Engines link,
          one line instead of a wrapped block that was taller than the model's
          own name.

          The drawer went with the chevron. It held the file count, the branch
          and the added date, plus a revision list; the added date is a row in
          the (i) panel, the other two were dropped as facts nobody comes to
          this page for, and revisions were abstracted away entirely — a repo
          holding two commits is simply a bigger number, and the Delete beside
          it is the same remedy it always was. `Revisions.tsx` and its endpoint
          went with it. */}
    </div>
  );
}