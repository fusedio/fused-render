// The Local tab: one list per capability holding what this machine HAS and what
// to get next, a search of the whole Hub above them, and the deletions that free
// the disk.
//
// **The whole answer, in one place.** A capability's list is the loaded model
// first, then what has been downloaded for it most recently, then the curation's
// recommendations with a Download button — so a fresh machine sees what to get
// exactly where what it has will appear.
//
// **And its SEARCH is here too** (D426): the controls sit at the top of this tab
// and the page has two mutually exclusive faces — an empty box is everything
// below, a query is ONE section of Hub results in place of all of it
// (`searchChrome`, `HubResults`). Never both.
//
// It manages that cache too (D250): delete a repo, after a confirmation that
// names its target; the dangerous arithmetic lives on the server.
//
// THE LISTING IS NOT THIS TAB'S. It arrives as `scan` from the page above
// (lib/useCacheScan.ts), because the walk is shared. What IS this tab's is the
// curation it joins onto that walk, which delete is pending, and what a delete
// that failed had to say.
import { useEffect, useRef, useState } from "react";
import { DeleteDialogs } from "./DeleteDialogs";
import { HubResults, type SettledQuery } from "./HubResults";
import { RecommendedRow } from "./RecommendedRow";
import { RepoRow } from "./RepoRow";
import { SearchControls } from "./SearchControls";
import { shortCommit } from "./hub";
import {
  curatedRepoIds,
  diskCards,
  groupRepos,
  loadRefusal,
  mergeSections,
  runnersByCapability,
} from "@apps/ai_models/lib/aiModelGroups";
import { publishAiRuntime, refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { searchChrome, type ResultSort } from "@apps/ai_models/lib/hubSearchView";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { ErrorNote } from "@apps/ai_models/shared/ErrorNote";
import {
  deleteAiModels,
  downloadAiModel,
  getAiCatalog,
  loadAiModel,
  unloadAiModel,
  type AiCatalogCapability,
  type AiModelDeleteTarget,
  type AiModelRepo,
} from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pushToast } from "@platform/lib/toast";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { Identifier, Muted, SectionHeading, SectionTitle } from "@platform/ui/flow/Typography";

/** What a confirmation is about. Every destructive action becomes one of these
 *  first — there is no path from a click straight to a delete. */
export type Pending = { kind: "repo"; repo: AiModelRepo };

export function LocalTab({ scan }: { scan: CacheScan }) {
  const { load, data, repos, loadedById, jobByModel, downloading, settling, scanEpoch, publishListing } = scan;
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  // Per-target refusals from the last delete. A banner rather than a toast: it
  // names things the user asked for and did not get.
  const [failures, setFailures] = useState<string[]>([]);
  // The curation. `null` until it has answered.
  const [catalog, setCatalog] = useState<AiCatalogCapability[] | null>(null);
  // The id this tab last pressed Download on, held until something else can
  // speak for the pull (`spokenFor`).
  const [starting, setStarting] = useState<string | null>(null);
  // The search, live. Held HERE because `settled` decides which face of the
  // page renders, and the ✕ and "Back to models" are one act (`clearSearch`).
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("");
  const [sort, setSort] = useState<ResultSort>("downloads");
  // …and settled: a burst of typing is one request, and the LAYOUT must not
  // swap on the first letter and back on a backspace.
  const [settled, setSettled] = useState<SettledQuery>({ q: "", task: "", sort: "downloads" });
  const debounce = useRef<number | null>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (debounce.current) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => setSettled({ q: query, task, sort }), 350);
    return () => {
      if (debounce.current) window.clearTimeout(debounce.current);
    };
  }, [query, task, sort]);

  /** Back to this machine's own models, in one act: BOTH inputs (D317). The
   *  sort is untouched. Focus returns to the box. */
  const clearSearch = () => {
    setQuery("");
    setTask("");
    searchBox.current?.focus();
  };

  // The curation, on the SAME trigger as the walk: a finished download changes
  // which models are worth recommending, and an engine switch changes the
  // whole shortlist.
  useEffect(() => {
    let alive = true;
    getAiCatalog().then(
      (cat) => alive && setCatalog(cat.capabilities),
      // A failed RE-fetch must not throw away a good catalog.
      () => alive && setCatalog((prev) => prev ?? []),
    );
    return () => {
      alive = false;
    };
  }, [scanEpoch]);

  // Which models already have a disk ROW on this tab, and which disk state each
  // is in (D424). ONE map, both faces of the page (D426). `null` until the walk
  // has answered.
  const onCard = data ? diskCards(repos) : null;

  // A curated id → the repo id that ADDRESSES its bytes (`AiCatalogModel.repo`).
  const repoById = new Map<string, string>(
    (catalog ?? []).flatMap((entry) => entry.models.map((m) => [m.id, m.repo ?? m.id] as const)),
  );

  // Repo id -> the curated `label` (AI-2c): a human display name is a CURATED
  // field, never one derived by stripping a repo id at runtime.
  const labelByRepoId = new Map<string, string>(
    (catalog ?? []).flatMap((entry) => entry.models.map((m) => [m.repo ?? m.id, m.label] as const)),
  );

  // The click is held until something ELSE can speak for the pull — the runtime
  // reporting it, `settling` carrying it through the walk, or the walk finding
  // it on disk. The third arm speaks the disk's language (repo ids).
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

  // The download manager's ✕. It is a REQUEST — the row stays until the worker
  // honours it.
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
      // The capability travels with the request: without it the API defaults
      // to text generation.
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
      // The endpoint answers with the fresh listing, so a delete costs no
      // second walk.
      publishListing(result);
      setFailures(
        result.failures.map(
          (f) => `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`,
        ),
      );
      pushToast({
        msg: result.freed ? `Freed ${formatSize(result.freed)} — ${label}` : `Nothing deleted — ${label}`,
        tone: result.failures.length ? "error" : "info",
      });
      setPending(null);
    } catch (e) {
      // A transport/guard failure never reached the disk — leave the dialog open.
      setFailures([(e as Error).message]);
    } finally {
      setBusy(false);
    }
  };

  // Which capability the curation files each model under — the fallback a
  // partly downloaded repo's resume needs.
  const capabilityById = new Map<string, string>(
    (catalog ?? []).flatMap((entry) => entry.models.map((m) => [m.id, entry.capability] as const)),
  );

  // Which repo ids the curation actually names — the seal beside a row's name.
  const curated = curatedRepoIds(catalog);

  const grouped = groupRepos(repos);
  // The two payloads joined into the rows the page draws: disk rows first
  // (loaded ones leading), then what the curation recommends.
  const sections = mergeSections(grouped.models.groups, catalog, loadedById, onCard);
  const runners = runnersByCapability(catalog);

  // Which face of the page is on screen: from the SETTLED query. The ✕ in the
  // box follows the LIVE controls, because it belongs to the box.
  const face = searchChrome(settled.q, settled.task).face;
  const live = searchChrome(query, task);

  /** The three-way guard, asked once, by both faces of the page. */
  const pulling = (id: string) => downloading.has(id) || starting === id || settling.has(id);

  // One row, wherever it ends up.
  const row = (r: AiModelRepo) => {
    // What a RESUME would be filed under. `unservable` overrules both and is
    // checked FIRST: the server is what knows no engine here can serve it.
    const resumeCapability = r.engine?.unservable ? null : r.capability ?? capabilityById.get(r.id) ?? null;
    return (
      <RepoRow
        key={r.path}
        repo={r}
        label={labelByRepoId.get(r.id)}
        curated={curated.has(r.id)}
        loaded={loadedById.get(r.id)}
        job={jobByModel.get(r.id)}
        busy={busy}
        fetching={downloading.has(r.id)}
        refusal={loadRefusal(r)}
        resumeCapability={resumeCapability}
        onDeleteRepo={() => setPending({ kind: "repo", repo: r })}
        onDownload={() => resumeCapability && runDownload(r.id, resumeCapability)}
        onCancel={runCancelDownload}
        onLoad={() => runLoad(r)}
        onUnload={() => runUnload(r)}
      />
    );
  };

  return (
    <>
      {/* At the TOP, above everything: the one control that changes what the
          whole page is. */}
      <SearchControls
        query={query}
        task={task}
        sort={sort}
        showsReset={live.showsReset}
        searchBox={searchBox}
        onQuery={setQuery}
        onTask={setTask}
        onSort={setSort}
        onClear={clearSearch}
      />
      {load.status === "error" && <ErrorNote>{load.message}</ErrorNote>}
      {runtimeError && <ErrorNote>{runtimeError}</ErrorNote>}
      {failures.length > 0 && (
        <ErrorNote>
          {failures.map((f) => (
            <div key={f}>{f}</div>
          ))}
        </ErrorNote>
      )}
      {/* ONE face at a time. The delete dialog stays mounted through both. */}
      {face === "results" ? (
        <HubResults
          settled={settled}
          cards={onCard}
          runners={runners}
          curated={curated}
          jobByModel={jobByModel}
          pulling={pulling}
          onDownload={runDownload}
          onCancel={runCancelDownload}
          onBack={clearSearch}
        />
      ) : (
        <>
          {load.status === "loading" && <Muted className="py-6 text-center">Reading the Hugging Face cache…</Muted>}
          {data &&
            (sections.length || grouped.components.repos.length ? (
              <>
                {/* Section A. One list per capability: what somebody downloaded
                    for it, then what the curation says to get. */}
                {sections.length > 0 && (
                  <section className="flex flex-col gap-4">
                    {/* "User downloaded models", not "Models": what distinguishes
                        this section from the one below is WHO asked for these. */}
                    <SectionTitle>User downloaded models</SectionTitle>
                    {sections.map((section) => (
                      <div className="flex flex-col gap-2" key={section.key}>
                        <div className="flex items-baseline gap-3">
                          <SectionHeading>{section.label}</SectionHeading>
                          {/* DISK bytes, and only disk bytes: a claim about this
                              machine that can be checked against the cache. */}
                          {section.size > 0 && <Identifier>{formatSize(section.size)}</Identifier>}
                        </div>
                        {section.note && <Muted>{section.note}</Muted>}
                        <EntityList>
                          {section.disk.map(row)}
                          {section.recommended.map((m) => (
                            <RecommendedRow
                              key={m.id}
                              model={m}
                              runner={section.runner}
                              busy={pulling(m.id)}
                              job={jobByModel.get(m.id)}
                              onDownload={() => runDownload(m.id, section.key)}
                              onCancel={runCancelDownload}
                            />
                          ))}
                        </EntityList>
                      </div>
                    ))}
                  </section>
                )}
                {/* Section B. No sub-grouping: the HEADING does the work, and
                    the rows already wear "part of X". */}
                {grouped.components.repos.length > 0 && (
                  <section className="flex flex-col gap-2">
                    <SectionTitle>Fetched by engines</SectionTitle>
                    <Muted>
                      Automatically downloaded by a runner. If deleted, it will be downloaded again on next run.
                    </Muted>
                    <EntityList>{grouped.components.repos.map(row)}</EntityList>
                  </section>
                )}
              </>
            ) : catalog === null ? (
              // The catalog has not answered, and on an empty cache it is the
              // only thing that can put a row on this page.
              <Muted className="py-6 text-center">Reading the model catalog…</Muted>
            ) : (
              // Two different nothings: no cache dir at all versus a cache that
              // has been emptied. Only reachable when the CURATION is empty too;
              // the search box above is still a way out.
              <Muted className="py-6 text-center">
                {data.exists
                  ? "Nothing cached here yet."
                  : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
              </Muted>
            ))}
        </>
      )}
      <DeleteDialogs pending={pending} busy={busy} onClose={() => setPending(null)} onConfirm={runDelete} />
    </>
  );
}
