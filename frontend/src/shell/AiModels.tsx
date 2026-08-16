// /ai-models: what the Hugging Face cache holds on this machine, and the
// deletions that free it.
//
// The cache is shared by everything that speaks huggingface_hub — a
// transformers import, a diffusers pipeline, an `hf download`, a page a user
// pasted in — and it is invisible: it fills up under ~/.cache with multi-GB
// checkpoints nothing on screen ever mentions. This page is the missing
// inventory: one card per cached repo, with what it costs on disk, its NAME
// linking to the model's page on the Hub and an "Explore" that opens it HERE —
// two destinations, so neither has to win the same click.
//
// Biggest-first WITHIN a group, not across the page (shell/aiModelGroups.ts).
// One flat size sort put a 2.4GB component a runner fetched for itself fifth,
// between two models the user chose, and left the distinction to the quietest
// chip on the card. Position now carries meaning: what you chose, by what it
// does; then what an engine fetched; then the repos nothing here recognises.
// Every group states its own byte subtotal, because a group that can be skipped
// must still say what it costs.
//
// It manages that cache too (D250), two ways: delete a repo, or delete one
// revision of one. Both name their targets in a confirmation the user reads
// first, and the dangerous arithmetic (which blobs a revision actually owns)
// lives on the server, where the filesystem is.
//
// Page chrome AND the cards are the cc-* family — cc-mdgrid/cc-mdcard, the same
// card the Claude config panel's MD Files section uses — so the shell's
// non-explorer pages read as one surface rather than each inventing a list.
// Only what those classes have no answer for is local (styles/ai-models.css):
// the size figure, the Explore link, the revision drawer, and the tab strip.
import { useEffect, useMemo, useRef, useState } from "react";
import AiModelsDiscover from "./AiModelsDiscover";
import AiModelsEngines from "./AiModelsEngines";
import { ModelProgress } from "./AiProgress";
import { groupRepos, loadRefusal, noEngineReason } from "@shell/aiModelGroups";
import { isBusy, publishAiRuntime, refreshAiRuntime, useAiRuntime } from "./aiRuntime";
import {
  deleteAiModels,
  getAiModelRevisions,
  getAiModels,
  type AiModelDeleteTarget,
  type AiModelRepo,
  type AiModelRevision,
  loadAiModel,
  unloadAiModel,
  type AiLoadedModel,
  type AiModelsResult,
} from "@platform/lib/api";
import { useNavEpoch, useRefreshOnReturn } from "@platform/lib/hooks";
import { fetchJobs, type Job } from "@platform/lib/jobs";
import { formatSize, formatMtimeFull, formatParams, timeAgo } from "@platform/lib/format";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";

// This page's sidebar entry is UNCONDITIONAL (HF-8, D265), so nothing here
// reports the cache's existence to anyone. It used to: a `./aiModelsAvailable`
// module held the gate's probe cache and this page published into it on load
// and after deleting the last repo, because a page that had just walked the
// cache knew the answer without a second request. The gate is gone and the
// module with it. `data.exists` stays, with the two readers it always had on
// this page: the caption, which only links a cache directory that is really
// there, and the empty state, which says WHICH nothing it found.

type Load =
  | { status: "loading" }
  | { status: "ok"; data: AiModelsResult }
  | { status: "error"; message: string };

// "Local", not "Cached": what the tab shows is the models this machine HAS, and
// "cached" describes the mechanism (a Hugging Face cache directory) rather than
// the thing. Discover is the other half of the same question — what it could
// have — and "local vs discover" is the pair that reads.
//
// Engines is the third, moved here from Preferences (D302 shipped it there).
// It is a setting, but every consequence of it is on this page — which cards
// can be loaded, what their engine tags say, what Discover suggests — and the
// question it answers ("why can't I load this?") is asked with the unloadable
// card on screen. `/preferences?tab=engines` is rewritten to it
// (`rewriteLegacyUrl`), so nobody's bookmark lands on a tab that is gone.
export type AiModelsTab = "local" | "discover" | "engines";

/** The tab the URL asks for. An unknown value falls back to the default
 *  silently, the same forgiving posture the shell takes for an unknown `_mode`
 *  (PT-9): a stale link should open the page, not an error. */
function tabFromUrl(): AiModelsTab {
  const asked = new URLSearchParams(location.search).get("tab");
  return asked === "discover" || asked === "engines" ? asked : "local";
}

// What the confirmation is about. Every destructive action becomes one of these
// first — there is no path from a click straight to a delete.
type Pending =
  | { kind: "repo"; repo: AiModelRepo }
  | { kind: "revision"; repo: AiModelRepo; revision: AiModelRevision };

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

function hubUrl(repo: AiModelRepo): string {
  const id = repo.id.split("/").map(encodeURIComponent).join("/");
  return `${HUB_ORIGIN}/${HUB_PREFIX[repo.kind]}${id}`;
}

function shortCommit(commit: string): string {
  // Cache directories are named by full sha; the first 7 are what anyone reads.
  return /^[0-9a-f]{16,}$/i.test(commit) ? commit.slice(0, 7) : commit;
}

// The revisions drawer: fetched per repo when a row is expanded, since
// resolving every snapshot symlink in every repo is exactly what the
// biggest-first overview avoids doing.
function Revisions({
  repo,
  inUse,
  onDelete,
}: {
  repo: AiModelRepo;
  /** Why deleting from this repo is refused right now, or "" — the same
   *  sentence the repo's own Delete uses. A revision is not the safer target it
   *  looks like: the one a resident worker holds open is the one it is reading
   *  from, so both buttons answer to the same rule (AI-5f). */
  inUse: string;
  onDelete: (revision: AiModelRevision) => void;
}) {
  const [rows, setRows] = useState<AiModelRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    // Dropped BEFORE the refetch, not replaced after it. These numbers are
    // relative to the other revisions: deleting one makes its siblings' shared
    // blobs exclusive, so every surviving row's "frees this much" grows the
    // moment a sibling goes. Holding the old rows through the refetch would
    // show understated sizes, and a delete clicked in that window would freeze
    // one into a confirmation that promised to free far less than it will.
    setRows(null);
    setError(null);
    getAiModelRevisions(repo.dir).then(
      (r) => alive && setRows(r.revisions),
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
    // Re-fetched when the repo's revision count changes under a deletion.
  }, [repo.dir, repo.revisions]);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!rows) return <div className="am-drawer-note">Reading revisions…</div>;
  if (!rows.length) return <div className="am-drawer-note">No revisions materialised.</div>;
  return (
    <div className="am-drawer">
      {rows.map((rev) => (
        <div className="am-rev" key={rev.commit}>
          <span className="am-rev-main">
            <span className="am-rev-commit cc-mono">{shortCommit(rev.commit)}</span>
            {rev.refs.map((ref) => (
              <span className="cc-pill" key={ref}>
                {ref}
              </span>
            ))}
            <span className="am-rev-meta">
              {rev.files} {rev.files === 1 ? "file" : "files"}
              {rev.shared ? ` · ${formatSize(rev.shared)} shared with other revisions` : ""}
            </span>
          </span>
          {/* The size that matters is what deleting THIS revision frees, not
              what it appears to contain — see the endpoint's docstring. */}
          <span className="am-rev-size" title="Freed by deleting this revision">
            {formatSize(rev.size)}
          </span>
          <button
            type="button"
            className="cc-iconbtn cc-iconbtn-danger"
            title={
              inUse
                ? `Cannot delete a revision of ${repo.id}: ${inUse}`
                : `Delete revision ${shortCommit(rev.commit)}`
            }
            aria-label={`Delete revision ${shortCommit(rev.commit)}`}
            disabled={!!inUse}
            onClick={() => onDelete(rev)}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
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
 *  **The CPU case is the one this exists for.** torch runs on whatever it can
 *  see, and on Windows the standard PyTorch build sees no GPU at all, so a
 *  perfectly healthy 4B model answers at a few words a second. Without this the
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
            "answer. On Windows the standard PyTorch build is CPU-only; " +
            "elsewhere it means no supported GPU was found."
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

function RepoCard({
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
  // Whether the revisions drawer has anything to show. Read twice — by the
  // expander and by the drawer's own guard below — from one name, so the two
  // cannot come apart.
  const hasRevisions = repo.revisions > 1;
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
                 visible label so the accessible name still contains what is on
                 screen (WCAG 2.5.3), which is what keeps "click the Diffusers
                 tag" a workable instruction for voice control. */
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
              {repo.engine.shortLabel}
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
        {repo.library && <span className="am-card-library">{repo.library}</span>}
      </div>
      {/* What this model is doing RIGHT NOW, as opposed to what it is. Absent
          when the answer is "sitting on disk", which is what every card would
          otherwise say — a row of identical chips carries no information. */}
      {(loaded || job) && <RuntimeChip loaded={loaded} job={job} />}
      <div className="cc-mdcard-foot">
        <span className="cc-mdcard-meta">
          {repo.files} {repo.files === 1 ? "file" : "files"}
          {repo.revisions > 1 ? ` · ${repo.revisions} revisions` : ""}
          {repo.refs.length ? ` · ${repo.refs.join(", ")}` : ""}
          {when ? ` · used ${when}` : ""}
          {added ? ` · added ${added}` : ""}
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
          {/* Disabled at a single revision, not removed. It does nothing there
              — deleting "the revision" and deleting the repo are the same act,
              and two controls for it would only ask the user to tell them apart
              — but a chevron that is present on some cards and absent on others
              is a difference the reader has to notice and then explain, and the
              explanation is a fact about the repo worth stating outright. */}
          <button
            type="button"
            className={"cc-iconbtn" + (expanded ? " cc-btn-on" : "")}
            disabled={!hasRevisions}
            title={
              hasRevisions
                ? expanded
                  ? "Hide revisions"
                  : "Show revisions"
                : `${repo.id} has one revision, so there is nothing to expand — deleting it and deleting the repo are the same act.`
            }
            aria-label={
              hasRevisions
                ? `${expanded ? "Hide" : "Show"} revisions of ${repo.id}`
                : `Revisions of ${repo.id} — only one, nothing to expand`
            }
            /* Only where it describes something. A disabled control that
               announces itself as collapsed invites the reader to expand it. */
            aria-expanded={hasRevisions ? expanded : undefined}
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
              <path d={expanded && hasRevisions ? "m6 15 6-6 6 6" : "m6 9 6 6 6-6"} />
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
      {/* Kept even though the expander is now always rendered: the expander
          being DISABLED at one revision is not the same as this drawer being
          closed, and a repo that drops to one revision under a deletion must
          collapse itself rather than strand an open drawer above a control that
          can no longer close it. */}
      {expanded && hasRevisions && <Revisions repo={repo} inUse={inUse} onDelete={onDeleteRevision} />}
    </div>
  );
}

/** A section heading with its own byte subtotal.
 *
 *  The subtotal is not decoration and it is not the same figure as the caption's
 *  total. This page's job is "what is eating my disk", and grouping introduced
 *  something the flat list did not have: a section a reader may reasonably
 *  decide to skip. A section that can be skipped has to state its cost on the
 *  way past, or the grouping hides exactly the number the page exists to show.
 */
function SectionHead({ title, size }: { title: string; size: number }) {
  return (
    <div className="am-section-head">
      <h3 className="am-section-title">{title}</h3>
      <span className="am-section-size" title={`${title} take up ${formatSize(size)} on this machine`}>
        {formatSize(size)}
      </span>
    </div>
  );
}

export default function AiModels() {
  // Local is the default, and Discover is the only thing on this page that
  // touches the network — so nothing is sent to a third party until someone
  // asks for it. The tab is not mounted until selected, which is also what
  // keeps the query from firing on page load.
  //
  // **The tab lives in the URL, not in state** (`?tab=discover`), the pattern
  // Preferences already uses for its own tabs: it makes the choice
  // bookmarkable and — the reason it is worth doing here — it puts the toggle
  // on the BACK BUTTON, which is where a user reaches for "put it back how it
  // was". `useNavEpoch` is the subscription: it counts pushState and popstate
  // alike, so a back out of Discover re-reads the URL and lands on Local.
  const navEpoch = useNavEpoch();
  const tab = useMemo(tabFromUrl, [navEpoch]);
  const setTab = (next: AiModelsTab) => {
    if (next === tab) return;
    const params = new URLSearchParams(location.search);
    // The default is the ABSENCE of the param, so /ai-models stays the URL for
    // the page rather than becoming a redirect to /ai-models?tab=local.
    if (next === "local") params.delete("tab");
    else params.set("tab", next);
    const search = params.toString();
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // What is resident, and the download-manager rows for anything mid-bring-up.
  // The runtime is polled by a shared subscriber (the sidebar dot reads the same
  // one); the job rows are read here because only this page joins them onto
  // cards, and only while something is actually running.
  const runtime = useAiRuntime();
  // Returning to this tab re-checks what's loaded RIGHT NOW rather than
  // waiting out the idle poll's 10s (aiRuntime.ts) — the same "cheap state,
  // re-read on return" posture as the deploy dot and account status
  // (lib/hooks.ts). Deliberately narrower than the disk walk below: that scan
  // is a filesystem crawl over every blob and stays gated on a KNOWN change
  // (a delete or a finished download), never on a focus tick.
  useRefreshOnReturn(refreshAiRuntime);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  // Per-target refusals from the last delete (a symlinked repo, a row that was
  // already gone). A banner rather than a toast: it names things the user asked
  // for and did not get.
  const [failures, setFailures] = useState<string[]>([]);
  // Bumped to re-walk the cache. Not a Refresh button (D256) — the two writers
  // are both the app noticing that the disk really changed: a finished download
  // is a new repo, and a page still showing "not downloaded" beside a finished
  // pull is the same lie the ✓-on-click bug was.
  const [scan, setScan] = useState(0);
  // Models whose pull has ended but whose confirming walk has not landed. For
  // that moment they are in neither the runtime's downloading list nor the
  // listing, and a card reading only those two put a Download button back on a
  // model that had just finished downloading.
  const [settling, setSettling] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    // A RE-walk keeps the listing on screen while it runs: replacing a good
    // grid with "Reading the cache…" because a download finished would make the
    // page flash for news that only adds one card.
    setLoad((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    getAiModels().then(
      (data) => {
        if (!alive) return;
        setLoad({ status: "ok", data });
      },
      (e: Error) => alive && setLoad({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
    // Scanning is a disk walk over every blob, so it runs once per mount and
    // then only when the disk is KNOWN to have changed — never on a focus/return
    // tick, which would re-walk tens of thousands of files every time the user
    // alt-tabbed back, and never behind a Refresh button, which asked the user
    // to know when a re-walk was worth it. A delete answers with the fresh
    // listing itself; a finished download bumps `scan`.
  }, [scan]);

  const anyBusy = isBusy(runtime);
  useEffect(() => {
    // Both tabs now: a Download started from Discover is a job row Discover
    // draws on its own cards, and gating the poll on the Local tab left those
    // cards frozen on "Starting…".
    // Only while something is live: the manager already polls these for its own
    // list, and a second poller on an idle machine is two requests a second for
    // an empty array.
    if (!anyBusy) {
      setJobs([]);
      return;
    }
    let alive = true;
    const tick = () => fetchJobs().then((s) => alive && setJobs(s.jobs), () => {});
    void tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [anyBusy]);

  // A download that has STOPPED being reported has landed (or failed), and
  // either way the disk is not what the last walk said it was. This is the one
  // honest trigger for a re-walk: the transition, not a timer and not a click.
  const downloadingKey = runtime.downloading
    .map((d) => d.model)
    .sort()
    .join(" ");
  const previousDownloads = useRef<string[]>([]);
  useEffect(() => {
    const now = downloadingKey ? downloadingKey.split(" ") : [];
    const before = previousDownloads.current;
    previousDownloads.current = now;
    // Only on a set that SHRANK. A set that GREW means a pull just started, and
    // a walk then would find exactly the disk the page already knows about.
    const finished = before.filter((model) => !now.includes(model));
    if (!finished.length) return;
    // The walk takes a moment, and for that moment the model is in NEITHER the
    // downloading list nor the listing — which is how a finished download got a
    // "Download" button back for a beat. It stays "finishing" until the walk it
    // just triggered answers for it.
    setSettling((s) => new Set([...s, ...finished]));
    setScan((n) => n + 1);
  }, [downloadingKey]);

  const data = load.status === "ok" ? load.data : null;

  useEffect(() => {
    // Any fresh listing settles every pending question: it either found the
    // model or it did not, and a failed or cancelled pull must not sit as
    // "finishing" for the rest of the session waiting for a success that is
    // not coming.
    if (data) setSettling((s) => (s.size ? new Set() : s));
  }, [data]);
  const repos = data?.repos ?? [];
  const loadedById = new Map(runtime.loaded.map((m) => [m.model, m]));
  // Matched by TITLE, which the supervisor sets to the model id, rather than by
  // re-deriving the job id here: that derivation sanitises characters, and a
  // second copy of the rule in TypeScript would drift from the Python one the
  // moment either changed.
  const jobByModel = new Map(
    jobs.filter((j) => j.owner === "server").map((j) => [j.title, j]),
  );
  // Loadable is ONE question, asked of the server: is there an engine that
  // reads this repo's format and runs on this machine. It used to be asked of
  // the capability alone — this repo has one, and some runner here serves it —
  // which is true of `openai/whisper-large-v3` on every machine and false of
  // every repo whose card was missing a task label. The format is the half that
  // was missing, and `repo.engine` carries both halves (see `_engine`).
  //
  // Asked through `loadRefusal` rather than as a boolean here, because the
  // button now needs the SENTENCE and not just the verdict — and one function
  // answering both is what stops a card that is disabled for one reason
  // explaining itself with another.
  // What Discover means by "you already have this one". A MATERIALISED snapshot,
  // not merely a folder: huggingface_hub creates `models--org--name/` the moment
  // a pull starts, so a set built from folder names alone flipped a suggestion
  // to "✓ downloaded" seconds after the Download button was pressed. It is the
  // same partial-vs-downloaded line the Hub result cards already draw.
  //
  // `null` until the walk has answered, so a card says neither "you have this"
  // nor "you don't" while the page still has no idea.
  const onDisk = data ? new Set(repos.filter((r) => r.revisions > 0).map((r) => r.id)) : null;
  const downloading = new Set(runtime.downloading.map((d) => d.model));

  const runLoad = async (repo: AiModelRepo) => {
    setRuntimeError(null);
    try {
      // The capability travels with the request: without it the API defaults to
      // text generation, and a diffusion model would be handed to the chat
      // runner.
      await loadAiModel(repo.id, repo.capability ?? undefined);
      refreshAiRuntime();
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
  };

  const runUnload = async (repo: AiModelRepo) => {
    setRuntimeError(null);
    try {
      publishAiRuntime(await unloadAiModel(repo.id));
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
  };

  const runDelete = async (targets: AiModelDeleteTarget[], label: string) => {
    setBusy(true);
    try {
      const result = await deleteAiModels(targets);
      setLoad({ status: "ok", data: result });
      setFailures(
        result.failures.map((f) => `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`),
      );
      // A deletion that freed nothing is worth saying out loud too — it means
      // every target failed, and the banner beside it says why.
      pushToast({
        msg: result.freed ? `Freed ${formatSize(result.freed)} — ${label}` : `Nothing deleted — ${label}`,
        tone: result.failures.length ? "error" : "info",
      });
      setPending(null);
    } catch (e) {
      // A transport/guard failure never reached the disk, so the listing on
      // screen is still true — surface it and leave the dialog open.
      setFailures([(e as Error).message]);
    } finally {
      setBusy(false);
    }
  };

  // Derived on every render rather than memoised: it is one pass over a list
  // whose length is the number of repos in a cache, and `repos` is a fresh array
  // each render anyway — a memo keyed on it would recompute every time and cost
  // the comparison on top.
  const grouped = groupRepos(repos);

  // One card, wherever it ends up. Written once because a section is only a
  // heading and a subset — nothing about a card changes with the group it is
  // drawn in, and two copies of this call site would be two places for a prop
  // to go missing.
  const card = (r: AiModelRepo) => (
    <RepoCard
      key={r.path}
      repo={r}
      expanded={expanded === r.dir}
      loaded={loadedById.get(r.id)}
      job={jobByModel.get(r.id)}
      busy={busy}
      fetching={downloading.has(r.id)}
      refusal={loadRefusal(r)}
      onToggle={() => setExpanded(expanded === r.dir ? null : r.dir)}
      onDeleteRepo={() => setPending({ kind: "repo", repo: r })}
      onDeleteRevision={(revision) => setPending({ kind: "revision", repo: r, revision })}
      onLoad={() => runLoad(r)}
      onUnload={() => runUnload(r)}
    />
  );

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">AI Models</h2>
            <div className="cc-caption cc-mono">
              {tab === "discover" ? (
                "Models on the Hugging Face Hub"
              ) : tab === "engines" ? (
                // Not the cache path: this tab is not about the disk, and a
                // caption naming a directory over a panel of engine pickers is
                // the page's chrome contradicting its content.
                "Which backend runs each kind of local model"
              ) : data ? (
                <>
                  {/* The path is a DESTINATION, not a label. It is the one
                      place on this page that answers "where has all this
                      actually gone", and the app is a file explorer — leaving
                      it as text asks the user to copy it into the thing they
                      are already looking at. A real <a href> so middle-click
                      and copy-link work, with left-click intercepted for
                      client-side navigation like every other in-app link. */}
                  {/* …but only where there is something to open. `exists:
                      false` means no download has ever created this directory,
                      and a link to a path that is not there is worse than
                      text: it looks like an answer and lands on an error. The
                      path is still SHOWN — it is where the models would go, and
                      the empty state below says so. */}
                  {data.exists ? (
                    <a
                      className="am-cache-dir"
                      href={urlForFsPath(data.cacheDir)}
                      title={`Open ${data.cacheDir} in the explorer`}
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
                        navigate(data.cacheDir, { isDir: true });
                      }}
                    >
                      {data.cacheDir}
                    </a>
                  ) : (
                    data.cacheDir
                  )}
                  {repos.length
                    ? ` · ${repos.length} cached · ${formatSize(data.totalSize)} total`
                    : ""}
                </>
              ) : (
                "Hugging Face cache"
              )}
            </div>
          </div>
          <div className="am-head-actions">
            <div className="am-tabs" role="tablist" aria-label="AI models">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "local"}
                className={"am-tab" + (tab === "local" ? " active" : "")}
                onClick={() => setTab("local")}
                title="Models already on this machine"
              >
                Local
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "discover"}
                className={"am-tab" + (tab === "discover" ? " active" : "")}
                onClick={() => setTab("discover")}
                title="Search the Hugging Face Hub for models you don't have yet"
              >
                Discover
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "engines"}
                className={"am-tab" + (tab === "engines" ? " active" : "")}
                onClick={() => setTab("engines")}
                title="Which backend runs each kind of local model"
              >
                Engines
              </button>
            </div>
          </div>
        </div>
        {tab === "discover" && (
          // The cache answer comes from the PAGE's walk, not from a second one
          // Discover runs for itself: one listing, one definition of "on this
          // machine", and no window where the two tabs disagree about the same
          // repo.
          <AiModelsDiscover
            onDisk={onDisk}
            downloading={downloading}
            settling={settling}
            jobByModel={jobByModel}
          />
        )}
        {tab === "engines" && <AiModelsEngines />}
        {tab === "local" && load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
        {tab === "local" && runtimeError && <ErrorBanner>{runtimeError}</ErrorBanner>}
        {tab === "local" && failures.length > 0 && (
          <ErrorBanner>
            {failures.map((f) => (
              <div key={f}>{f}</div>
            ))}
          </ErrorBanner>
        )}
        {tab === "local" && load.status === "loading" && (
          <p className="cc-empty">Reading the Hugging Face cache…</p>
        )}
        {tab === "local" &&
          data &&
          (repos.length ? (
            <>
              {/* Section A. Everything somebody chose to download, under the
                  capability that would serve it. Rendered at all only when
                  there is one — a machine holding nothing but a runner's own
                  components should not be told it has a Models section. */}
              {grouped.models.groups.length > 0 && (
                <section className="am-section">
                  <SectionHead title="Models" size={grouped.models.size} />
                  {grouped.models.groups.map((group) => (
                    <div className="am-subgroup" key={group.key}>
                      <div className="am-subgroup-head">
                        <h4 className="am-subgroup-title">{group.label}</h4>
                        <span className="am-subgroup-size">{formatSize(group.size)}</span>
                      </div>
                      {group.note && <p className="am-group-note">{group.note}</p>}
                      <div className="cc-mdgrid am-grid">{group.repos.map(card)}</div>
                    </div>
                  ))}
                </section>
              )}
              {/* Section B. No sub-grouping: there are a handful of these, and
                  it is the HEADING that does the work now — the cards already
                  wear "part of X", and scattering them through a size-sorted
                  list is what made that chip the only thing distinguishing a
                  2.4GB machine-fetched repo from a model the user picked. */}
              {grouped.components.repos.length > 0 && (
                <section className="am-section">
                  <SectionHead title="Fetched by engines" size={grouped.components.size} />
                  <p className="am-group-note">
                    Downloaded by a runner to do its job — nobody chose these. They are listed
                    and deletable because they eat the same disk as everything above; delete one
                    and the next run that needs it fetches it again.
                  </p>
                  <div className="cc-mdgrid am-grid">{grouped.components.repos.map(card)}</div>
                </section>
              )}
            </>
          ) : (
            // Two different nothings: no cache dir at all (nothing has ever
            // pulled from the Hub) versus a cache that has been emptied. The
            // path itself is already in the caption above, so it isn't repeated
            // here.
            //
            // Either nothing ends in the SAME next move, which is why the
            // sidebar entry no longer has to guess whether this page is worth
            // offering (HF-8, D265): Discover is right here, and a machine with
            // no cache is precisely the one that needs it. A button rather than
            // a sentence naming the tab — the tab strip is at the top of the
            // page and the empty state is in the middle of it, so "use
            // Discover" would be an instruction where a control fits.
            <div className="cc-empty am-empty">
              <p>
                {data.exists
                  ? "Nothing cached here yet."
                  : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
              </p>
              <button type="button" className="btn btn-secondary" onClick={() => setTab("discover")}>
                Search the Hub
              </button>
            </div>
          ))}
      </main>

      {pending?.kind === "repo" && (
        <Modal
          title={`Delete ${pending.repo.id}?`}
          busy={busy}
          onClose={() => setPending(null)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() => runDelete([{ dir: pending.repo.dir }], `deleted ${pending.repo.id}`)}
              >
                {busy ? "Deleting…" : `Delete · ${formatSize(pending.repo.size)}`}
              </button>
            </>
          }
        >
          <p>
            Removes every revision of <b>{pending.repo.id}</b> from this machine and frees{" "}
            <b>{formatSize(pending.repo.size)}</b>. Anything that needs it again downloads it again.
          </p>
          <p className="cc-mono cc-unset">{pending.repo.path}</p>
        </Modal>
      )}

      {pending?.kind === "revision" && (
        <Modal
          title={`Delete revision ${shortCommit(pending.revision.commit)}?`}
          busy={busy}
          onClose={() => setPending(null)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() =>
                  runDelete(
                    [{ dir: pending.repo.dir, revision: pending.revision.commit }],
                    `deleted ${pending.repo.id} @ ${shortCommit(pending.revision.commit)}`,
                  )
                }
              >
                {busy ? "Deleting…" : `Delete · ${formatSize(pending.revision.size)}`}
              </button>
            </>
          }
        >
          <p>
            Removes revision <span className="cc-mono">{shortCommit(pending.revision.commit)}</span>{" "}
            of <b>{pending.repo.id}</b>, freeing <b>{formatSize(pending.revision.size)}</b>.
            {pending.revision.shared > 0 && (
              <>
                {" "}
                The <b>{formatSize(pending.revision.shared)}</b> it shares with the other revisions
                stays.
              </>
            )}
          </p>
          {pending.revision.refs.length > 0 && (
            <p>
              {pending.revision.refs.join(", ")}{" "}
              {pending.revision.refs.length === 1 ? "points" : "point"} at this revision and will be
              removed with it.
            </p>
          )}
          {pending.repo.revisions === 1 && (
            <p>It is the only revision left, so the whole repo folder goes.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
