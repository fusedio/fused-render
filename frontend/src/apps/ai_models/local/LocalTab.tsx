// The Local tab: a capability column on the left (`CapabilityNav`) and, on the
// right, everything about whichever one is selected — what this machine HAS,
// what to get next, and a door into a full search of the whole Hub
// (`CapabilityPane` / `HubSearchScreen`), or the one bucket that is not a
// capability at all, the engine files a runner fetched to do its job
// (`EngineFilesPane`).
//
// This is the two-pane port of the page the carousel/table layout used to be.
// The MERGE underneath is unchanged (`aiModelGroups.ts`, D424/D426): one map
// of on-disk truth (`diskCards`) serves both the capability pane's rows and
// the Hub search results, so "you already have this one" has one definition
// per page, and `mergeSections` still decides which rows a capability has and
// in what order. What moved is only the SKIN: a section used to be a
// carousel of `RepoCard`/`RecommendedCard`; it is now `ModelRowModel` rows
// drawn by the shared `ModelRow`, and the Hub search used to replace the
// whole page (`HubResults`) — it now replaces one pane, reached from that
// pane's own "Search Hugging Face for more…" link and left the same way
// (`onBack`), never handing back a different capability than the one it was
// opened from.
//
// **Selecting a capability resets its pane's own transient state** — the
// search screen closes, the (i) drawer closes — the same reset the approved
// mockup's own nav click-handler performs (`st.search = 'closed'; st.info =
// null`), because a capability nobody is looking at should not be quietly
// answering a search or holding a drawer open underneath it.
//
// It still manages the cache too (D250): delete a repo, both ways in named
// only after a confirmation the user reads first (`DeleteDialogs`), the
// dangerous arithmetic (which blobs a revision actually owns) staying on the
// server, where the filesystem is.
//
// THE LISTING IS NOT THIS TAB'S. It arrives as `scan` from the page above
// (lib/useCacheScan.ts), because the walk is shared — see there. What IS this
// tab's is everything below: the curation it joins onto that walk, which
// capability is selected, which row's drawer is open, which delete is
// pending, and what a delete that failed had to say.
import { useEffect, useState } from "react";
import { CapabilityNav, type CapabilityNavEntry } from "./CapabilityNav";
import { CapabilityPane, EngineFilesPane, type EngineFilePart } from "./CapabilityPane";
import { DeleteDialogs } from "./DeleteDialogs";
import { HubSearchScreen, type SettledQuery } from "./HubSearchScreen";
import { type ModelRowHandlers, type ModelRowModel, type ModelRowProgress } from "./ModelRow";
import { shortCommit } from "./hub";
import {
  curatedRepoIds,
  diskCards,
  groupRepos,
  mergeSections,
  PARTIAL_TAG,
  resumable,
} from "@apps/ai_models/lib/aiModelGroups";
import { refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { activeFitLevel, activeParamsBand, activeSort, type ResultSort } from "@apps/ai_models/lib/hubSearchView";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import {
  deleteAiModels,
  downloadAiModel,
  getAiCatalog,
  loadAiModel,
  revealPath,
  type AiCatalogCapability,
  type AiCatalogModel,
  type AiModelDeleteTarget,
  type AiModelRepo,
  type HubFitLevel,
  type HubParamsBand,
} from "@platform/lib/api";
import { formatParams, formatSize, repoName, timeAgo } from "@platform/lib/format";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { notify } from "@platform/lib/notifications";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

/** What a confirmation is about. Every destructive action becomes one of these
 *  first — there is no path from a click straight to a delete. */
export type Pending = { kind: "repo"; repo: AiModelRepo };

/** Everything the URL says about the Hub search, read once on load —
 *  `hubQ`/`hubTask`/`hubSort` unconditionally, and Part B's four
 *  search-only facets (`hubFit`/`hubParams`/`hubQuant`/`hubOrg`) ONLY when a
 *  query or task is ALSO present. See `SettledQuery` (HubSearchScreen.tsx)
 *  for the shape this fills in.
 *
 *  Prefixed names throughout (`hub*`, never bare `q`/`sort`/`model`):
 *  `?model=` already means something else, page-wide (the side panel's own
 *  seed — a live, deliberately unfixed collision this page must not add a
 *  second version of).
 *
 *  No `hubUnfit`/`includeUnfit` any more (item 7, D843 round 5): the "Show
 *  models that will not fit" toggle is gone — every row is always shown, the
 *  per-row red "Will not fit" line is the only warning now. An old URL still
 *  carrying `?hubUnfit=1` is simply ignored.
 */
function readHubUrl(): SettledQuery {
  const q = readParam("hubQ") ?? "";
  const task = readParam("hubTask") ?? "";
  const asked = !!(q.trim() || task.trim());
  return {
    q,
    task,
    sort: activeSort(readParam("hubSort") as ResultSort).value,
    fitLevel: asked ? activeFitLevel(readParam("hubFit") as HubFitLevel).value : "any",
    paramsBand: asked ? activeParamsBand(readParam("hubParams") as HubParamsBand).value : "any",
    quant: asked ? (readParam("hubQuant") ?? "") : "",
    publisher: asked ? (readParam("hubOrg") ?? "") : "",
  };
}

const BLANK_SEARCH: Omit<SettledQuery, "sort"> = {
  q: "",
  task: "",
  fitLevel: "any",
  paramsBand: "any",
  quant: "",
  publisher: "",
};

/** One disk repo, as the row skeleton wants it. The curation, when it names
 *  this repo (`catalogByRepoId`), supplies the display name, the "Our pick"
 *  flag, the fit verdict and the note the drawer shows under "Why we suggest
 *  it" — a repo the curation has never heard of falls back to a mechanical
 *  split of its id (`repoName`) and carries no note, same as `ModelRow`'s own
 *  "Not one of our suggestions" branch expects.
 *
 *  A partly downloaded repo (D424, `resumable`) wears `PARTIAL_TAG` as its
 *  warning chip in place of a fit verdict — there is nothing to warn about
 *  memory for a download that never finished, and the partial state is the
 *  more urgent fact. `ModelRowModel` has no dedicated partial state of its
 *  own (unlike the carousel-era `RepoCard`), so this is the row's only way to
 *  say it; Try on a partial row is not disabled at this layer, and a click
 *  ends in the ordinary runtime-error banner rather than a refusal chip —
 *  documented as a deviation in this unit's own report.
 */
function diskRow(
  repo: AiModelRepo,
  catalogByRepoId: ReadonlyMap<string, AiCatalogModel>,
  curated: ReadonlySet<string>,
): ModelRowModel {
  const cat = catalogByRepoId.get(repo.id);
  return {
    id: repo.id,
    name: cat?.nickname || cat?.label || repoName(repo.id),
    curated: curated.has(repo.id),
    ourPick: !!cat?.recommended,
    warnChip: resumable(repo)
      ? PARTIAL_TAG
      : cat?.fit?.verdict === "no"
        ? `Needs ${formatSize(cat.fit.footprintBytes)}`
        : null,
    fit: resumable(repo) ? null : (cat?.fit?.verdict ?? null),
    have: true,
    sizeLabel: formatSize(repo.size),
    usedLabel: timeAgo(repo.lastUsed),
    engine: repo.engine?.shortLabel ?? null,
    params: repo.params !== null ? formatParams(repo.params) : null,
    quant: repo.quantization ?? null,
    format: "safetensors",
    fileCount: repo.files,
    path: repo.path,
  };
}

/** One curated-and-not-on-disk entry, as the row skeleton wants it. `engine`
 *  is the section's own runner — the backend that WOULD load this model,
 *  since nothing on disk yet can answer for itself. */
function catalogRow(m: AiCatalogModel, engine: string | null): ModelRowModel {
  return {
    id: m.id,
    name: m.nickname || m.label,
    curated: true,
    ourPick: m.recommended,
    warnChip: m.fit?.verdict === "no" ? `Needs ${formatSize(m.fit.footprintBytes)}` : null,
    fit: m.fit?.verdict ?? null,
    have: false,
    sizeLabel: m.size_gb ? formatSize(m.size_gb * 1024 ** 3) : "size not checked yet",
    usedLabel: null,
    engine,
    params: m.params ?? null,
    quant: m.quantization ?? null,
    format: "safetensors",
    fileCount: null,
    path: null,
  };
}

/** The id of the most recently used repo in a capability's `have` list, for
 *  the "Last used" chip — or null when nothing here has ever been used
 *  (a fresh download, or a noatime volume). */
function mostRecentId(repos: readonly AiModelRepo[]): string | null {
  let best: AiModelRepo | null = null;
  for (const r of repos) {
    if (r.lastUsed === null) continue;
    if (!best || (best.lastUsed ?? -1) < r.lastUsed) best = r;
  }
  return best?.id ?? null;
}

/** How long a job of this shape has left, in the one word `ModelRow` puts
 *  after "about" — a plain rate estimate (bytes moved so far, over the time
 *  spent moving them), because `Job` (jobs.ts) carries no ETA field of its
 *  own and nothing else in this app computes one. Never a fabricated number:
 *  a job too young to have a rate yet, or reporting anything but bytes,
 *  reads as the generic "a minute" rather than a precise-looking guess. */
function etaText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "a minute";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

/** A live job, read as the progress bar `ModelRow` draws — or null when the
 *  job cannot say (no byte total, e.g. a venv build or a weights load rather
 *  than a download in progress; `jobFraction` in `aiModelGroups.ts` applies
 *  the same gate for the same reason). */
function progressFor(job: Job | undefined): ModelRowProgress | null {
  if (!job || job.unit !== "bytes" || job.total === null || job.done === null || job.total <= 0) return null;
  const fraction = Math.min(1, Math.max(0, job.done / job.total));
  const elapsed = Date.now() / 1000 - job.started_at;
  const remaining = Math.max(0, job.total - job.done);
  const rate = elapsed > 1 && job.done > 0 ? job.done / elapsed : 0;
  return {
    doneLabel: formatSize(job.done),
    totalLabel: formatSize(job.total),
    etaLabel: rate > 0 ? etaText(remaining / rate) : "a minute",
    fraction,
  };
}

export function LocalTab({ scan }: { scan: CacheScan }) {
  const {
    load,
    data,
    repos,
    loadedById,
    jobByModel,
    downloading,
    settling,
    scanEpoch,
    publishListing,
  } = scan;
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  // Per-target refusals from the last delete (a symlinked repo, a row that was
  // already gone). A banner rather than a toast: it names things the user asked
  // for and did not get.
  const [failures, setFailures] = useState<string[]>([]);
  // The curation: which models to recommend per capability, and which backend
  // would load them. `null` until it has answered — the recommended rows say
  // nothing rather than an empty row while it is in flight.
  const [catalog, setCatalog] = useState<AiCatalogCapability[] | null>(null);
  // The id this tab last pressed Download on, held until something else can
  // speak for the pull (see `spokenFor`). Named `starting` because `pending`
  // here is the delete confirmation.
  const [starting, setStarting] = useState<string | null>(null);
  // Which capability's pane is on screen — a capability tag, "parts" for the
  // Engine files bucket, or null before the first render has anything to
  // default to.
  const [selected, setSelected] = useState<string | null>(null);
  // Whether the selected capability's pane is showing its own full Hub search
  // screen in place of the ordinary pane. Meaningless (and never true) while
  // `selected === "parts"` — there is no search door on that pane.
  const [searching, setSearching] = useState(() => {
    const url = readHubUrl();
    return !!(url.q.trim() || url.task.trim());
  });
  // The id of the row whose (i) disclosure drawer is open, or null. Scoped to
  // one row at a time, same as the mockup's own `st.info`.
  const [openInfoId, setOpenInfoId] = useState<string | null>(null);
  // The Hub search's settled query — seeded from the URL so a shared link
  // RUNS the search rather than merely prefilling the box (see `readHubUrl`).
  // `HubSearchScreen` owns its own live-typing debounce; this is only ever
  // written back to by `onSettle`, never per keystroke.
  const [settled, setSettled] = useState<SettledQuery>(readHubUrl);

  // Mirrors the SETTLED search into the URL. The four facets are cleared from
  // the URL (passed `null`) whenever nothing is asked — the WRITE-side mirror
  // of the idle pane not offering them: the URL must never advertise a filter
  // that is not in effect on whatever is actually on screen.
  useEffect(() => {
    const asked = !!(settled.q.trim() || settled.task.trim());
    writeParams({
      hubQ: settled.q || null,
      hubTask: settled.task || null,
      // "best" (D780) is the default, so it is the value omitted from the URL
      // rather than always written.
      hubSort: settled.sort === "best" ? null : settled.sort,
      hubFit: asked && settled.fitLevel !== "any" ? settled.fitLevel : null,
      hubParams: asked && settled.paramsBand !== "any" ? settled.paramsBand : null,
      hubQuant: asked && settled.quant ? settled.quant : null,
      hubOrg: asked && settled.publisher ? settled.publisher : null,
    });
  }, [settled]);

  // The curation, on the SAME trigger as the walk above and for the same two
  // reasons the walk has it. A finished download changes which models are worth
  // recommending, and an engine switch replaces the whole shortlist.
  useEffect(() => {
    let alive = true;
    getAiCatalog().then(
      (cat) => alive && setCatalog(cat.capabilities),
      () => alive && setCatalog((prev) => prev ?? []),
    );
    return () => {
      alive = false;
    };
  }, [scanEpoch]);

  // Which models already have a disk CARD on this tab, and which of the two
  // disk states each is in (D424/D426) — see `aiModelGroups.ts`'s own doc on
  // `diskCards` for why this is one map for both the pane rows and the Hub
  // search results.
  const onCard = data ? diskCards(repos) : null;

  // A curated id → the repo id that ADDRESSES its bytes (`AiCatalogModel.repo`),
  // needed wherever a model id meets `onCard`, whose keys are repo ids.
  const repoById = new Map<string, string>(
    (catalog ?? []).flatMap((entry) => entry.models.map((m) => [m.id, m.repo ?? m.id] as const)),
  );

  // The same repo id → the curated entry itself, first-wins — everything a row
  // needs from the curation (display name, note, fit, "our pick") in one
  // lookup, keyed the same way `curatedRepoIds`/`diskCards` are.
  const catalogByRepoId = new Map<string, AiCatalogModel>();
  for (const entry of catalog ?? []) {
    for (const m of entry.models) {
      const key = m.repo ?? m.id;
      if (!catalogByRepoId.has(key)) catalogByRepoId.set(key, m);
    }
  }

  // Every repo on this disk, by id — what a row's handler resolves a click
  // back to (Try, Delete, Show in Finder), whichever capability's pane it is
  // drawn in.
  const reposById = new Map<string, AiModelRepo>(repos.map((r) => [r.id, r]));

  // The click is held until something ELSE can speak for the pull — see the
  // long-form doc this reasoning used to carry in the carousel-era file;
  // unchanged here.
  const spokenFor =
    starting !== null &&
    (downloading.has(starting) || settling.has(starting) || !!onCard?.has(repoById.get(starting) ?? starting));
  useEffect(() => {
    if (spokenFor) setStarting(null);
  }, [spokenFor]);

  const runDownload = async (model: string, capability: string) => {
    setRuntimeError(null);
    setStarting(model);
    try {
      await downloadAiModel(model, capability);
      refreshAiRuntime();
    } catch (e) {
      setRuntimeError((e as Error).message);
      setStarting(null);
    }
  };

  const runCancelDownload = async (job: Job) => {
    setRuntimeError(null);
    try {
      await cancelJob(job.id);
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
    refreshAiRuntime();
  };

  const runLoad = async (repo: AiModelRepo) => {
    setRuntimeError(null);
    try {
      await loadAiModel(repo.id, repo.capability ?? undefined);
      refreshAiRuntime();
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
  };

  const runDelete = async (targets: AiModelDeleteTarget[], label: string) => {
    setBusy(true);
    try {
      const result = await deleteAiModels(targets);
      publishListing(result);
      setFailures(
        result.failures.map(
          (f) => `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`,
        ),
      );
      // A deletion that freed nothing is worth saying out loud too — it means
      // every target failed, and the banner beside it says why.
      //
      // "Freed 1.4 GB — deleted…" was this migration's own named motivating
      // example for destructive-but-successful trail-tier retention — now
      // reversed (user: "don't keep this in the list ... anything non
      // actionable or error doesn't belong in the list", see
      // DECISIONS-toasts-become-notifications.md): a clean run only pops,
      // via the plain tone: "info" default (transient). A failed run stays
      // attention — tone: "error" already promotes it there regardless of
      // tier, so no explicit override is needed on that branch.
      notify({
        title: result.freed
          ? `Freed ${formatSize(result.freed)} — ${label}`
          : `Nothing deleted — ${label}`,
        tone: result.failures.length ? "error" : "info",
      });
      setPending(null);
    } catch (e) {
      setFailures([(e as Error).message]);
    } finally {
      setBusy(false);
    }
  };

  const curated = curatedRepoIds(catalog);
  const grouped = groupRepos(repos);
  const sections = mergeSections(grouped.models.groups, catalog, loadedById, onCard);

  /** The three-way guard, asked once: a pull is live if the runtime reports it,
   *  if this tab just clicked it, or if it has stopped being reported and the
   *  confirming walk has not landed. */
  const pulling = (id: string) => downloading.has(id) || starting === id || settling.has(id);

  // Default the selection once there is something to select — a capability
  // with disk or recommended rows, else the Engine files bucket. Guarded on
  // `selected === null` so this fires exactly once and never overrides a
  // reader's own click.
  useEffect(() => {
    if (selected !== null) return;
    if (sections.length === 0 && grouped.components.repos.length === 0) return;
    // A shared link that asked for a search names its capability in
    // `settled.task` — land there with the search screen already open rather
    // than on the first section with the box empty.
    const askedCapability =
      searching && settled.task && sections.some((s) => s.key === settled.task) ? settled.task : null;
    if (askedCapability) setSelected(askedCapability);
    else if (sections.length > 0) setSelected(sections[0].key);
    else setSelected("parts");
  });

  const selectCapability = (key: string) => {
    setSelected(key);
    setSearching(false);
    setOpenInfoId(null);
  };

  const deleteRepo = (id: string) => {
    const r = reposById.get(id);
    if (r) setPending({ kind: "repo", repo: r });
  };

  const handlers: ModelRowHandlers = {
    onTry: (id) => {
      const r = reposById.get(id);
      if (r) void runLoad(r);
    },
    onDownload: (id) => {
      if (selected && selected !== "parts") void runDownload(id, selected);
    },
    onStop: (id) => {
      const job = jobByModel.get(id);
      if (job) void runCancelDownload(job);
    },
    onDelete: deleteRepo,
    onToggleInfo: (id) => setOpenInfoId((cur) => (cur === id ? null : id)),
    onShowInFinder: (id) => {
      const r = reposById.get(id);
      if (r) void revealPath(r.path);
    },
  };

  const capabilities: CapabilityNavEntry[] = sections.map((s) => ({
    key: s.key,
    count: s.disk.length,
    runner: s.runner,
  }));

  const section = selected && selected !== "parts" ? (sections.find((s) => s.key === selected) ?? null) : null;
  const have = section ? section.disk.map((r) => diskRow(r, catalogByRepoId, curated)) : [];
  const lastUsedId = section ? mostRecentId(section.disk) : null;
  const recommended = section ? section.recommended.map((m) => catalogRow(m, section.runner?.shortLabel ?? null)) : [];
  const downloadingId = recommended.find((m) => pulling(m.id))?.id ?? null;
  const downloadProgress = downloadingId ? progressFor(jobByModel.get(downloadingId)) : null;
  const offReason = section?.runner && !section.runner.available ? section.runner.reason : null;

  const parts: EngineFilePart[] = grouped.components.repos.map((r) => ({
    id: r.id,
    name: r.component?.file ?? repoName(r.id),
    partOf: r.component?.owner ?? "",
    sizeLabel: formatSize(r.size),
    usedLabel: timeAgo(r.lastUsed),
  }));

  /** Leaving the search screen also drops the search itself (D317's own
   *  rule, scoped here to a pane rather than to the whole tab): a reader who
   *  goes back should find the pane exactly as it was, not a stale query
   *  still sitting in the URL for the next capability they open. The sort is
   *  left alone, same as the carousel-era `clearSearch` did — it is a
   *  standing preference, not part of one search. */
  const backToPane = () => {
    setSearching(false);
    setSettled((prev) => ({ ...BLANK_SEARCH, sort: prev.sort }));
  };

  return (
    <>
      {load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
      {runtimeError && <ErrorBanner>{runtimeError}</ErrorBanner>}
      {failures.length > 0 && (
        <ErrorBanner>
          {failures.map((f) => (
            <div key={f}>{f}</div>
          ))}
        </ErrorBanner>
      )}
      {load.status === "loading" && <p className="cc-empty">Reading the Hugging Face cache…</p>}
      {data && catalog === null && <p className="cc-empty">Reading the model catalog…</p>}
      {data && catalog !== null && (
        sections.length === 0 && grouped.components.repos.length === 0 ? (
          <p className="cc-empty">
            {data.exists
              ? "Nothing cached here yet."
              : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
          </p>
        ) : (
          <div className="tp" data-part="page">
            <CapabilityNav
              capabilities={capabilities}
              partsCount={grouped.components.repos.length}
              selected={selected}
              onSelect={selectCapability}
            />
            {selected === "parts" ? (
              <EngineFilesPane parts={parts} totalBytes={grouped.components.size} onDelete={deleteRepo} />
            ) : selected && searching ? (
              <HubSearchScreen
                capabilityKey={selected}
                settled={settled}
                cards={onCard}
                jobByModel={jobByModel}
                pulling={pulling}
                onDownload={runDownload}
                onCancel={runCancelDownload}
                onQuery={() => {}}
                onSettle={setSettled}
                onBack={backToPane}
              />
            ) : selected ? (
              <CapabilityPane
                key={selected}
                capabilityKey={selected}
                have={have}
                lastUsedId={lastUsedId}
                recommended={recommended}
                downloadingId={downloadingId}
                downloadProgress={downloadProgress}
                openInfoId={openInfoId}
                totalBytes={section?.size ?? 0}
                offReason={offReason ?? null}
                handlers={handlers}
                onOpenSearch={(capabilityKey) => {
                  // Item A: land in the search screen already asking for this
                  // capability's own task, rather than the blank "Any task" —
                  // the capability key IS the Hub's own pipeline tag (see
                  // `capabilityMeta.ts`'s doc on `CAPABILITY_ORDER`), so no
                  // separate lookup table is needed here.
                  setSettled((prev) => ({ ...prev, task: capabilityKey }));
                  setSearching(true);
                }}
              />
            ) : null}
          </div>
        )
      )}
      <DeleteDialogs pending={pending} busy={busy} onClose={() => setPending(null)} onConfirm={runDelete} />
    </>
  );
}
