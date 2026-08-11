// /ai-models: what the Hugging Face cache holds on this machine, and the
// deletions that free it.
//
// The cache is shared by everything that speaks huggingface_hub — a
// transformers import, a diffusers pipeline, an `hf download`, a page a user
// pasted in — and it is invisible: it fills up under ~/.cache with multi-GB
// checkpoints nothing on screen ever mentions. This page is the missing
// inventory: one card per cached repo, biggest first, with what it costs on
// disk, its NAME linking to the model's page on the Hub and an "Explore" that
// opens it HERE — two destinations, so neither has to win the same click.
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
import { useEffect, useRef, useState } from "react";
import AiModelsDiscover from "./AiModelsDiscover";
import { ModelProgress } from "./AiProgress";
import { isBusy, publishAiRuntime, refreshAiRuntime, useAiRuntime } from "./aiRuntime";
import {
  deleteAiModels,
  getAiModelRevisions,
  getAiModels,
  getAiModelsStatus,
  type AiModelDeleteTarget,
  type AiModelRepo,
  type AiModelRevision,
  loadAiModel,
  unloadAiModel,
  type AiLoadedModel,
  type AiModelsResult,
} from "@platform/lib/api";
import { fetchJobs, type Job } from "@platform/lib/jobs";
import { formatSize, formatMtimeFull, formatParams, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";

// Sidebar gate. Availability is "does the hub cache dir exist", which — unlike
// the Claude config bridge's install-shaped answer — CAN flip mid-session: the
// first model a user ever downloads creates it. So a confirmed `true` is cached
// for the session (the row must not blink out when the shell swaps sidebars),
// while a `false` is only cached for PROBE_TTL_MS and re-probed by the next
// mount after it lapses. That bounds the cost to roughly one isdir() a minute
// for the majority of machines that have no cache at all.
//
// The answer is PUBLISHED rather than just stored, because the two writers are
// not the mounted sidebar: a probe from another mount, and the page's own load
// (which knows the truth without a second request), both have to reach a
// sidebar that is already on screen. Without that, opening /ai-models by URL
// on a machine whose cache appeared this session would update the cache and
// leave the entry missing until something remounted the sidebar. Deleting the
// last repo publishes too — the cache DIRECTORY survives an empty cache, so the
// entry stays, which is correct: the page still has a true thing to say.
const PROBE_TTL_MS = 60_000;
let cached: { available: boolean; at: number } | null = null;
const gateListeners = new Set<(available: boolean) => void>();

function publishAvailable(available: boolean) {
  cached = { available, at: Date.now() };
  for (const listener of gateListeners) listener(available);
}

export function useAiModelsAvailable(): boolean {
  const [available, setAvailable] = useState(cached?.available ?? false);
  useEffect(() => {
    gateListeners.add(setAvailable);
    // An answer that landed between this render and this effect (another
    // mount's probe resolving) would otherwise be missed.
    if (cached) setAvailable(cached.available);
    if (!cached || (!cached.available && Date.now() - cached.at >= PROBE_TTL_MS)) {
      getAiModelsStatus().then(
        (s) => publishAvailable(s.available),
        () => {
          // A failed probe is not a cached "no": leave the last known answer
          // (and the absent cache entry) alone so a transient fetch failure
          // neither hides a shown entry nor suppresses the next probe.
        },
      );
    }
    return () => {
      gateListeners.delete(setAvailable);
    };
  }, []);
  return available;
}

type Load =
  | { status: "loading" }
  | { status: "ok"; data: AiModelsResult }
  | { status: "error"; message: string };

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
  onDelete,
}: {
  repo: AiModelRepo;
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
            title={`Delete revision ${shortCommit(rev.commit)}`}
            aria-label={`Delete revision ${shortCommit(rev.commit)}`}
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
function RuntimeChip({ loaded, job }: { loaded?: AiLoadedModel; job?: Job }) {
  if (loaded?.state === "ready") {
    // The badge above already said "loaded"; this row carries the one thing a
    // badge cannot — the number. Nothing at all when the worker could not
    // measure itself, rather than a row that repeats the badge.
    if (!loaded.residentBytes) return null;
    return (
      <div className="am-card-runtime am-card-runtime-ready">
        <span
          className="am-runtime-mem am-runtime-mem-lead"
          title={
            "Resident memory of the model's process. Not the model's size: it " +
            "counts shared pages too and moves while it generates."
          }
        >
          {formatSize(loaded.residentBytes)} in memory
        </span>
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
  canLoad,
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
  /** False when no runner here serves this kind of model — the control is then
   *  not offered at all, rather than offered and always failing. */
  canLoad: boolean;
  onToggle: () => void;
  onDeleteRepo: () => void;
  onDeleteRevision: (revision: AiModelRevision) => void;
  onLoad: () => void;
  onUnload: () => void;
}) {
  const when = timeAgo(repo.lastUsed ?? repo.mtime);
  // "added", not "released": the Hub's release date isn't on this disk (see the
  // endpoint), so the card states the date this machine actually knows.
  const added = timeAgo(repo.added);
  const live = loaded?.state === "ready";
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
        <span className="cc-pill">{repo.kind}</span>
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
      {(repo.task || repo.params) && (
        <div className="am-card-what">
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
      )}
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
              rather than disk. Only offered for a capability this machine can
              actually serve: on a Windows box the text runner is unavailable,
              and a button that always fails is worse than no button. */}
          {!canLoad ? null : loaded ? (
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
              disabled={busy || !!job}
              title={`Load ${repo.id} into memory so it can answer`}
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
          {/* Only offered where it means something: with a single revision,
              deleting "the revision" and deleting the repo are the same act,
              and two controls for it would just ask the user to tell them
              apart. */}
          {repo.revisions > 1 && (
            <button
              type="button"
              className={"cc-iconbtn" + (expanded ? " cc-btn-on" : "")}
              title={expanded ? "Hide revisions" : "Show revisions"}
              aria-label={`${expanded ? "Hide" : "Show"} revisions of ${repo.id}`}
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
          )}
          <button
            type="button"
            className="cc-iconbtn cc-iconbtn-danger"
            title={`Delete ${repo.id}`}
            aria-label={`Delete ${repo.id}`}
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
      {/* Same predicate as the expander above, so a repo that drops to one
          revision under a deletion collapses itself rather than stranding an
          open drawer with no control left to close it. */}
      {expanded && repo.revisions > 1 && <Revisions repo={repo} onDelete={onDeleteRevision} />}
    </div>
  );
}

export default function AiModels() {
  // Cached is the default, and Discover is the only thing on this page that
  // touches the network — so nothing is sent to a third party until someone
  // asks for it. The tab is not mounted until selected, which is also what
  // keeps the query from firing on page load.
  const [tab, setTab] = useState<"cached" | "discover">("cached");
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // What is resident, and the download-manager rows for anything mid-bring-up.
  // The runtime is polled by a shared subscriber (the sidebar dot reads the same
  // one); the job rows are read here because only this page joins them onto
  // cards, and only while something is actually running.
  const runtime = useAiRuntime();
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
        // The page's own answer is authoritative for the sidebar gate: a cache
        // that exists (or has just appeared) shouldn't wait out the probe TTL,
        // and a sidebar already on screen hears this immediately. Published
        // BEFORE the `alive` check, deliberately: the gate is shared state, and
        // the sidebar it is for outlives this page (navigating between shell
        // routes unmounts the page and keeps ShellSidebar mounted). A scan the
        // user navigated away from still learned the truth — dropping it would
        // hide a real cache for the rest of the TTL. Only the local setState,
        // which belongs to a component that may be gone, sits behind the guard.
        publishAvailable(data.exists);
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
    // draws on its own cards, and gating the poll on the cached tab left those
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
  // Loadable means TWO things, and conflating them was a bug: this repo has a
  // capability at all (a dataset, an embedding model or a vision-language model
  // has none), and a runner here serves that capability. The repo's capability
  // comes from the server, which owns both vocabularies.
  const servable = new Set(
    runtime.runners.filter((r) => r.available).map((r) => r.capability),
  );
  const canLoad = (repo: AiModelRepo) =>
    !!repo.capability && servable.has(repo.capability);
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
      publishAvailable(result.exists);
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

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">AI Models</h2>
            <div className="cc-caption cc-mono">
              {tab === "discover"
                ? "Models on the Hugging Face Hub"
                : data
                  ? `${data.cacheDir}${repos.length ? ` · ${repos.length} cached · ${formatSize(data.totalSize)}` : ""}`
                  : "Hugging Face cache"}
            </div>
          </div>
          <div className="am-head-actions">
            <div className="am-tabs" role="tablist" aria-label="AI models">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "cached"}
                className={"am-tab" + (tab === "cached" ? " active" : "")}
                onClick={() => setTab("cached")}
              >
                Cached
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
        {tab === "cached" && load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
        {tab === "cached" && runtimeError && <ErrorBanner>{runtimeError}</ErrorBanner>}
        {tab === "cached" && failures.length > 0 && (
          <ErrorBanner>
            {failures.map((f) => (
              <div key={f}>{f}</div>
            ))}
          </ErrorBanner>
        )}
        {tab === "cached" && load.status === "loading" && (
          <p className="cc-empty">Reading the Hugging Face cache…</p>
        )}
        {tab === "cached" &&
          data &&
          (repos.length ? (
            <div className="cc-mdgrid am-grid">
              {repos.map((r) => (
                <RepoCard
                  key={r.path}
                  repo={r}
                  expanded={expanded === r.dir}
                  loaded={loadedById.get(r.id)}
                  job={jobByModel.get(r.id)}
                  busy={busy}
                  canLoad={canLoad(r)}
                  onToggle={() => setExpanded(expanded === r.dir ? null : r.dir)}
                  onDeleteRepo={() => setPending({ kind: "repo", repo: r })}
                  onDeleteRevision={(revision) => setPending({ kind: "revision", repo: r, revision })}
                  onLoad={() => runLoad(r)}
                  onUnload={() => runUnload(r)}
                />
              ))}
            </div>
          ) : (
            // Two different nothings: no cache dir at all (nothing has ever
            // pulled from the Hub) versus a cache that has been emptied. The
            // path itself is already in the caption above, so it isn't repeated
            // here.
            <p className="cc-empty">
              {data.exists
                ? "Nothing cached here yet."
                : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
            </p>
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
