// One cached repo, as a card: what it is, what it costs, what it is doing right
// now, and the four controls that act on it (Load/Unload, Try, Explore,
// Delete) — plus the drawer the chevron opens.
//
// Split out of the page file, where it and its three helper chips were ~570 of
// the Local tab's lines. Nothing about a card is page state: everything it
// draws arrives as a prop, and everything it does leaves as a callback, which
// is what let the page keep ONE call site for it (`card()` in LocalTab) across
// two differently-grouped sections.
import { Revisions } from "./Revisions";
import { hubUrl } from "./hub";
import { ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { unloadCountdown } from "@apps/ai_models/lib/engines";
import {
  type AiLoadedModel,
  type AiModelRepo,
  type AiModelRevision,
} from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";
import { formatSize, formatMtimeFull, formatParams, timeAgo } from "@platform/lib/format";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { noEngineReason } from "@apps/ai_models/lib/aiModelGroups";
import { tabHref } from "@apps/ai_models/routes";

/** Where the Try button goes: the playground, with this model selected and
 *  nothing else carried over. */
function tryHref(repo: AiModelRepo): string {
  return tabHref("playground", "?model=" + encodeURIComponent(repo.id));
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
      title={
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
      title={
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

function RuntimeChip({ loaded, job }: { loaded?: AiLoadedModel; job?: Job }) {
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
            title={
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
      <div className="am-card-runtime am-card-runtime-error" title={loaded.error ?? undefined}>
        Failed to load{loaded.error ? ` — ${loaded.error}` : ""}
      </div>
    );
  }
  return <ModelProgress detail={loaded?.detail} job={job} />;
}

export function RepoCard({
  repo,
  expanded,
  loaded,
  job,
  busy,
  fetching,
  refusal,
  onToggle,
  onDeleteRepo,
  onDeleteRevision,
  onLoad,
  onUnload,
}: {
  repo: AiModelRepo;
  expanded: boolean;
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
  onToggle: () => void;
  onDeleteRepo: () => void;
  onDeleteRevision: (revision: AiModelRevision) => void;
  onLoad: () => void;
  onUnload: () => void;
}) {
  // Whether the drawer has a REVISION list in it. The drawer itself always has
  // something to show now (the facts the card's face no longer carries), so
  // this no longer gates the expander — only the list inside it.
  const hasRevisions = repo.revisions > 1;
  // Whether the ENGINE tag is the one actually drawn on this card — a
  // component wears "part of X" instead, and a dataset wears nothing. Read by
  // the format chip below, which exists only to answer a question the engine
  // tag already answered when it is there.
  const showsEngineTag = !repo.component && repo.kind === "model" && !!repo.engine;
  const when = timeAgo(repo.lastUsed ?? repo.mtime);
  // "added", not "released": the Hub's release date isn't on this disk (see the
  // endpoint), so the card states the date this machine actually knows.
  const added = timeAgo(repo.added);
  const live = loaded?.state === "ready";
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
  return (
    <div className={"cc-mdcard am-card" + (live ? " am-card-loaded" : "")}>
      <div className="cc-mdcard-head">
        {/* The NAME goes to the HUB. A repo id is a Hub address, and the page
            it names is where the licence, the full model card, the discussions
            and every revision live — none of which this machine has. Opening it
            HERE is a different act with a different destination, so it gets its
            own control in the footer instead of competing for the same click.
            Still a real <a href>, so middle-click and copy-link behave. */}
        <a
          className="cc-mdcard-name am-card-name"
          href={hubUrl(repo)}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${repo.id} on the Hugging Face Hub`}
        >
          {repo.id}
        </a>
        {loaded?.state === "ready" && <LoadedBadge loaded={loaded} />}
        {/* Only when the kind is NOT the one the page already promises. A page
            titled "AI Models" listing eight cards each tagged MODEL states the
            obvious eight times and spends head-row width doing it. A dataset or
            a Space in the same cache is the exception the reader has to notice —
            it is not loadable, its Hub address is a different one (HUB_PREFIX),
            and the tag is the only thing on the card that says so. */}
        {repo.kind !== "model" && <span className="cc-pill">{repo.kind}</span>}
        {/* The size is the reason this page exists, so it is a figure in the
            card's head rather than another clause in the meta line. */}
        <span
          className="am-card-size"
          title={repo.mtime ? `Last changed ${formatMtimeFull(repo.mtime)}` : undefined}
        >
          {formatSize(repo.size)}
        </span>
      </div>
      {/* What the model is FOR, and how big it is — the two questions a name
          alone doesn't answer. Absent entirely when the download brought no
          evidence for either, rather than rendered as an empty line: a repo
          whose weights are a .bin pickle and whose card never came down really
          is a repo we can only name. */}
      {/* Always rendered, and that is deliberate twice over. The ENGINE line is
          the one fact this page could not answer — a repo belongs to a backend,
          not to a capability — and "nothing here reads this" is its most useful
          state rather than an edge case worth hiding. Rendering it
          unconditionally also gives every card the same number of rows, which
          is what stops a metadata-less card collapsing and taking its
          neighbours' footers out of line. */}
      <div className="am-card-what">
        {/* A repo the user never downloaded on purpose wears WHOSE it is,
            instead of an engine tag. "no engine" was true of both of these and
            explained neither: the 2.4GB GGUF is FLUX's transformer and deleting
            it breaks that model, the 2MB Silero is the whisper engine's speech
            detector and deleting it only costs speed.

            The tag no longer has to carry the whole distinction on its own —
            these cards sit under "Fetched by engines" now, and that heading is
            what stops a component reading as a model. What the tag adds is WHICH
            one it belongs to, and the hover adds what deleting THIS one costs,
            which differs per component and is not in the heading. That is prose
            nothing else on the card repeats, so it keeps its tab stop the same
            way the unavailable engine tag does. */}
        {repo.component ? (
          <span
            className="am-card-engine am-card-engine-component"
            tabIndex={0}
            aria-label={`Part of ${repo.component.owner} — ${repo.component.what}`}
            title={repo.component.what}
          >
            part of {repo.component.owner}
          </span>
        ) : (
          repo.kind === "model" &&
          (repo.engine ? (
            <span
              className={
                "am-card-engine" + (repo.engine.available ? "" : " am-card-engine-off")
              }
              /* Focusable only in the state that has something to say. The
                 unavailable tag reads the same as the available one now, so
                 its reason is carried by the hover — and a hover on a span
                 nothing can focus does not exist for a keyboard or a screen
                 reader. The available tag's title is a nicety, not the only
                 copy of anything, so it does not earn a tab stop on every
                 card. */
              tabIndex={repo.engine.available ? undefined : 0}
              /* Says the STATE in words, because the tag's own text no longer
                 does and colour must not be the only signal. It opens with the
                 hardware-qualified name, which STARTS WITH the family the tag
                 renders — that is why the accessible name still contains what
                 is on screen (WCAG 2.5.3) even though the two strings differ,
                 and why "click the Diffusers tag" stays a workable instruction
                 for voice control. `family_label` being a prefix of
                 `short_label` is asserted in the registry's own tests. */
              aria-label={
                repo.engine.available
                  ? undefined
                  : `${repo.engine.shortLabel} — cannot be loaded here: ${repo.engine.reason ?? "unavailable"}`
              }
              title={
                repo.engine.available
                  ? `Loads in the ${repo.engine.shortLabel} engine — read from the weight format on disk, which is the same check that engine makes before it loads.`
                  /* "cannot be loaded here", NOT "that engine cannot run
                     here": unavailable covers two different situations and the
                     second one is a preference, not a platform. A Diffusers
                     repo on a Mac whose image engine is set to MLX FLUX gets
                     `available: false` with a reason that ends "switch it in
                     Preferences" — Diffusers runs on that machine perfectly
                     well. Asserting the platform verdict in the prose flatly
                     contradicted the reason printed straight after it. */
                  : `This is a ${repo.engine.shortLabel} model, and it cannot be loaded here: ${repo.engine.reason ?? "unavailable"}.`
              }
            >
              {/* The FAMILY, not the build. An engine tag IS a format claim
                  (see the library tag below, which stands down when this one
                  is present), and all three Diffusers rows read the identical
                  safetensors — so "(ROCm)" here answers nothing a reader could
                  ask about a file on disk, and puts this machine's
                  configuration into a sentence about the model. The build is
                  not lost: the title and aria-label above name the
                  hardware-qualified engine, which is where a reader who has
                  stopped to ask reads it. */}
              {repo.engine.familyLabel}
            </span>
          ) : (
            <span
              className="am-card-engine am-card-engine-none"
              /* Same reasoning as above: "no engine" states its condition in
                 words, but WHY is hover-only, so it gets a tab stop too. */
              tabIndex={0}
              /* Asked of `aiModelGroups`, which is where the Load refusal and
                 the Unrecognised heading get their answer too. The tag is worn
                 by two different cards — a Qwen checkpoint in a format nothing
                 reads, and a repo nothing here can identify at all — and a
                 hardcoded sentence here said "weight format" to both, under a
                 heading and beside a button that had stopped saying it. */
              title={noEngineReason(repo)}
            >
              no engine
            </span>
          ))
        )}
        {repo.task && (
          // The hover answers both questions the label raises: what the task
          // MEANS ("image + text to text" is jargon until someone says it
          // takes a picture and a prompt), and where it came from — a
          // pipeline_tag is the Hub's own answer while an architecture is our
          // reading of one, which matters when the label looks wrong.
          <span
            className="am-card-task"
            title={
              [repo.taskHelp, repo.taskSource && `Read from ${repo.taskSource}.`]
                .filter(Boolean)
                .join(" ") || undefined
            }
          >
            {repo.task}
          </span>
        )}
        {repo.params !== null && (
          <span
            className="am-card-params"
            title={
              repo.paramsEstimated
                ? `≈${repo.params.toLocaleString()} parameters — unpacked from ${repo.quantization} weights, so it rests on the width the checkpoint declares`
                : `${repo.params.toLocaleString()} parameters`
            }
          >
            {/* The "≈" is doing real work: for a packed checkpoint the count
                is recovered arithmetic, not a measurement of unpacked
                shapes. */}
            {repo.paramsEstimated ? "≈" : ""}
            {formatParams(repo.params)} params
          </span>
        )}
        {repo.quantization && (
          <span
            className="am-card-quant"
            title={
              `Weights are stored at ${repo.quantization} each instead of the usual 16, ` +
              "so the download is a fraction of the full-precision one — cheaper to run, " +
              "slightly less accurate."
            }
          >
            {repo.quantization}
          </span>
        )}
        {/* The weight FORMAT, and only where nothing else on the row already
            said it. An engine tag IS a format claim — "MLX LM" is exactly the
            statement that these weights are mlx, which is also why the tag
            renders the engine FAMILY and leaves the accelerator to the hover —
            so printing both put the
            word "MLX" on the card three times (tag, format, `mlx-community/`
            in the name) and told the reader nothing on the second and third.
            A repo with no engine tag is the case this survives for: there the
            library is the only evidence of what the download actually is. */}
        {repo.library && !showsEngineTag && (
          <span className="am-card-library">{repo.library}</span>
        )}
      </div>
      {/* What this model is doing RIGHT NOW, as opposed to what it is. Absent
          when the answer is "sitting on disk", which is what every card would
          otherwise say — a row of identical chips carries no information. */}
      {(loaded || job) && <RuntimeChip loaded={loaded} job={job} />}
      <div className="cc-mdcard-foot">
        {/* ONE fact, not five. "15 files · main · used 4h ago · added 4h ago"
            was four numbers competing for the same glance, and only one of
            them is ever the reason someone is looking at this grid: how long
            it has been since anything read this. The file count, the branch
            and the added date are still HERE — they moved into the drawer the
            chevron beside this already opens (see below), because they are
            answers to a question about one repo rather than facts to sweep a
            grid with. */}
        <span className="cc-mdcard-meta" title={added ? `Added ${added}` : undefined}>
          {when ? `used ${when}` : ""}
        </span>
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
          {loaded ? (
            <button
              type="button"
              className="am-card-power am-card-power-on"
              disabled={busy}
              title={`Unload ${repo.id} and give its memory back`}
              onClick={onUnload}
            >
              Unload
            </button>
          ) : (
            <button
              type="button"
              className="am-card-power"
              disabled={busy || !!job || !!refusal}
              title={refusal ?? `Load ${repo.id} into memory so it can answer`}
              /* The reason again, in the accessible name. A `title` is a hover,
                 and a disabled button is one a pointer user may never think to
                 hover — while a screen reader reads the name and nothing else.
                 It opens with the visible label so the name still contains
                 what is on screen (WCAG 2.5.3). */
              aria-label={
                refusal ? `Load ${repo.id} — unavailable: ${refusal}` : `Load ${repo.id}`
              }
              onClick={onLoad}
            >
              {job ? "Loading…" : "Load"}
            </button>
          )}
          {/* Into the Playground, pre-selected — the tab whose whole job is
              "use it now". Only where the playground could actually serve it:
              the same loadability verdict the Load button rests on, since a
              model the resolved engine refuses would land on a sidebar that
              silently falls back to a different model. A real <a href>, like
              Explore beside it. */}
          {repo.capability && !refusal && (
            <a
              className="cc-iconbtn am-card-try"
              /* An EXPLICIT search, not the current one: `tabHref`'s default is
                 to carry the query across a tab switch (see routes.ts), which
                 is right for the strip and wrong here — this link's whole job
                 is to REPLACE whatever model the playground had selected with
                 this card's. Anything else in the query is a stage setting
                 dialled in for a different model. */
              href={tryHref(repo)}
              title={`Try ${repo.id} in the Playground`}
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
            >
              Try
            </a>
          )}
          {/* The local door: the model card view (SPEC §38), read from this
              folder's own files. A real <a href> so middle-click and copy-link
              work, with left-click intercepted for client-side navigation like
              every other in-app link. The folder LISTING stays the default
              everywhere else, because a gated template can never be a default
              mode (CT-12) — so this asks for the mode by name. */}
          <a
            className="cc-iconbtn am-card-explore"
            href={urlForFsPath(repo.path, "?_mode=model_card")}
            title={`Explore ${repo.id} here — ${repo.path}`}
            aria-label={`Explore ${repo.id}`}
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
              navigate(repo.path, { isDir: true, mode: "model_card" });
            }}
          >
            {/* An arrow into a box: "open this here", the same weight and box as
                the ✕ next to it rather than a word competing with it. */}
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M15 3h6v6" />
              <path d="M10 14 21 3" />
              <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
            </svg>
          </a>
          {/* No longer ever disabled: every card has details to open now (the
              file count, the branch and the added date the face gave up), and
              a repo with more than one revision gets its revision list under
              them. It used to be dead at a single revision, which was honest
              while "revisions" was the only thing behind it. */}
          <button
            type="button"
            className={"cc-iconbtn" + (expanded ? " cc-btn-on" : "")}
            title={expanded ? `Hide details of ${repo.id}` : `Show details of ${repo.id}`}
            aria-label={`${expanded ? "Hide" : "Show"} details of ${repo.id}`}
            aria-expanded={expanded}
            onClick={onToggle}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d={expanded ? "m6 15 6-6 6 6" : "m6 9 6 6 6-6"} />
            </svg>
          </button>
          <button
            type="button"
            className="cc-iconbtn cc-iconbtn-danger"
            title={inUse ? `Cannot delete ${repo.id}: ${inUse}` : `Delete ${repo.id}`}
            aria-label={`Delete ${repo.id}`}
            disabled={!!inUse}
            onClick={onDeleteRepo}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />
            </svg>
          </button>
        </span>
      </div>
      {/* The drawer. Everything the card's face used to state in a four-clause
          meta line, plus the revision list when there is more than one — the
          facts are not gone, they are one click away instead of on every card
          in the grid at once. */}
      {expanded && (
        <>
          <div className="am-drawer-facts">
            <span>
              {repo.files} {repo.files === 1 ? "file" : "files"}
            </span>
            {repo.revisions > 1 && <span>{repo.revisions} revisions</span>}
            {repo.refs.length > 0 && <span>{repo.refs.join(", ")}</span>}
            {added && <span>added {added}</span>}
          </div>
          {hasRevisions && <Revisions repo={repo} inUse={inUse} onDelete={onDeleteRevision} />}
        </>
      )}
    </div>
  );
}