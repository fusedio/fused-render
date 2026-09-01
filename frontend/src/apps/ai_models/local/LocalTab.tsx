// The Local tab: one row per capability holding what this machine HAS and what
// to get next, a search of the whole Hub above them, and the deletions that free
// the disk.
//
// **The whole answer, in one place.** A capability's row is the loaded model
// first, then what has been downloaded for it most recently, then the curation's
// recommendations with a Download button — so a fresh machine sees what to get
// exactly where what it has will appear, and the page fills up in place instead
// of sending anybody to a second grid on another tab. That is why there is no
// Discover tab (routes.ts): its answer is here now.
//
// **And its SEARCH is here too** (D426), which is the half the merge first left
// behind: a curated shortlist cannot answer "the model I read about this
// morning", and the only surface that could was a tab nothing linked to. So the
// controls sit at the top of this tab and the page has two mutually exclusive
// faces — an empty box is everything below, a query is ONE section of Hub
// results in place of all of it (`searchChrome`, `HubResults`). Never both: the
// carousels answer the question you have before you know what to type, and
// showing both grids asks the reader which one is talking to them.
//
// The search face is not a second implementation of anything. It borrows this
// tab's download plumbing prop for prop (`runDownload`, `jobByModel`, the
// three-way busy guard), this tab's card (`RecommendedCard`'s skeleton), and —
// the join that makes an in-app Hub search worth having at all — this tab's
// LISTING, so "you already have this one" has one definition per page.
//
// Position carries meaning both ways. ACROSS the page: what you chose, by what
// it does; then what an engine fetched ("Fetched by engines"); then the repos
// nothing here recognises. WITHIN a row: resident, then most recently used, then
// not here yet. Every capability states its own byte subtotal, because a row
// that can be skipped must still say what it costs — and that subtotal is DISK
// bytes only (see `MergedSection.size`).
//
// It manages that cache too (D250), two ways: delete a repo, or delete one
// revision of one. Both name their targets in a confirmation the user reads
// first, and the dangerous arithmetic (which blobs a revision actually owns)
// lives on the server, where the filesystem is.
//
// THE LISTING IS NOT THIS TAB'S. It arrives as `scan` from the page above
// (lib/useCacheScan.ts), because the walk is shared — see there. What IS this
// tab's is everything below: the curation it joins onto that walk, which card is
// expanded, which delete is pending, and what a delete that failed had to say.
import { useEffect, useRef, useState } from "react";
import { Carousel } from "./Carousel";
import { DeleteDialogs } from "./DeleteDialogs";
import { HubResults, type SettledQuery } from "./HubResults";
import { RecommendedCard } from "./RecommendedCard";
import { RepoCard } from "./RepoCard";
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
import { ErrorBanner } from "@platform/ui/ErrorBanner";

/** What a confirmation is about. Every destructive action becomes one of these
 *  first — there is no path from a click straight to a delete. */
export type Pending = { kind: "repo"; repo: AiModelRepo };

/** A section heading, and no figure beside it.
 *
 *  It carried a byte subtotal, on the argument that a section a reader may skip
 *  has to state its cost on the way past. Three levels of arithmetic said
 *  otherwise on the rendered page: the caption's "33 GB total", a section
 *  subtotal, and a per-capability subtotal, each correct and none of them the
 *  one being looked for. **The subtotals that survive are the ones next to the
 *  cards they are about** — the ALL-CAPS capability rows, which are the level a
 *  reader actually decides to skip at — with the page total in the caption
 *  above. The two figures in between were the sum of one and the parts of the
 *  other, restated in the middle.
 */
function SectionHead({ title }: { title: string }) {
  return (
    <div className="am-section-head">
      <h3 className="am-section-title">{title}</h3>
    </div>
  );
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
  // The search, live. Held HERE rather than inside `SearchControls` because
  // `settled` decides which face of the page renders, and because the ✕ in the
  // box and the "← Back to models" control in the results heading are one act
  // reachable from two places (`clearSearch`).
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("");
  // The PAGE's sort, which is a superset of the Hub's: "Size" is ranked here
  // because the Hub's list endpoint cannot rank by it (`ResultSort`). What
  // reaches the wire is `wireSort` of this, in `HubResults`.
  const [sort, setSort] = useState<ResultSort>("downloads");
  // Whether to ask for models this machine likely cannot run too — off by
  // default (D316's own "never a silent drop", applied here the way it was to
  // gated repos): the results summary states how many were hidden either way.
  const [includeUnfit, setIncludeUnfit] = useState(false);
  // …and settled, which is what the results section is keyed on: a burst of
  // typing is one request, and the LAYOUT must not swap on the first letter and
  // back on a backspace.
  const [settled, setSettled] = useState<SettledQuery>({
    q: "",
    task: "",
    sort: "downloads",
    includeUnfit: false,
  });
  const debounce = useRef<number | null>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (debounce.current) window.clearTimeout(debounce.current);
    // Long enough that a typed word is one request rather than five, short
    // enough that the results feel like they are following the query.
    debounce.current = window.setTimeout(
      () => setSettled({ q: query, task, sort, includeUnfit }),
      350,
    );
    return () => {
      if (debounce.current) window.clearTimeout(debounce.current);
    };
  }, [query, task, sort, includeUnfit]);

  /** Back to this machine's own models, in one act.
   *
   *  BOTH inputs, and that is the whole requirement (D317). A control that
   *  emptied the text and left the task filter set would leave the reader
   *  looking at an empty search box, a page of results, and no models — having
   *  done exactly what the page told them to. The sort is deliberately
   *  untouched: it does not push the page out of its own face, so resetting it
   *  would be the button doing something nobody asked for.
   *
   *  Focus returns to the box because it is where the next thing happens, and
   *  because two of the three controls that call this are it and a key pressed
   *  inside it.
   */
  const clearSearch = () => {
    setQuery("");
    setTask("");
    // Unlike the sort, this IS reset: it is a filter over one search's
    // answer, not a standing preference, and a reader who opted into "show
    // everything" on one query should not have it silently follow them into
    // the next unrelated one.
    setIncludeUnfit(false);
    searchBox.current?.focus();
  };

  // The curation, on the SAME trigger as the walk above and for the same two
  // reasons the walk has it. A finished download changes which models are worth
  // recommending (the one that just landed is not one of them any more), and an
  // engine switch changes the whole shortlist — the catalog's suggestions are
  // per runner, so picking MLX over torch replaces the list rather than
  // reordering it. Riding `scanEpoch` is what keeps those two answers from being
  // one poll apart: a recommended card and the repo card that replaces it are
  // drawn from two payloads, and a window where both were on screen would be one
  // model appearing twice.
  useEffect(() => {
    let alive = true;
    getAiCatalog().then(
      (cat) => alive && setCatalog(cat.capabilities),
      // A catalog that cannot be read costs the recommendations and nothing
      // else, so it is not a banner — the disk half of every row is still true.
      // `?? []` rather than `[]`: a failed RE-fetch must not throw away a good
      // catalog the page is already drawing.
      () => alive && setCatalog((prev) => prev ?? []),
    );
    return () => {
      alive = false;
    };
  }, [scanEpoch]);

  // Which models already have a disk CARD on this tab, and which of the two disk
  // states each is in — what a recommendation is filtered against, what a search
  // result reports about this machine, and what settles a held Download click.
  //
  // Not "this machine HAS the model": a download cancelled halfway is not a model
  // anybody can load (the server's `partial` says so, and `cached_models()` drops
  // it), but its card is right there wearing that state and carrying its own
  // Download — so recommending the same model beside it, or offering a fresh pull
  // for it in the search results, would draw one model twice (D424). The reading
  // lives in `aiModelGroups` next to the merge that consumes it.
  //
  // ONE map, both faces of the page (D426). The search reply's own `local` field
  // is deliberately unread: it is frozen at the moment of the search, so a model
  // downloaded from those very results would go on claiming to be absent.
  //
  // `null` until the walk has answered, so a card says neither "you have this"
  // nor "you don't" while the tab still has no idea.
  const onCard = data ? diskCards(repos) : null;

  // A curated id → the repo id that ADDRESSES its bytes (`AiCatalogModel.repo`),
  // which is the id itself for everything but a llama.cpp GGUF. Needed wherever
  // a model id meets `onCard`, whose keys are repo ids — see `mergeSections`.
  const repoById = new Map<string, string>(
    (catalog ?? []).flatMap((entry) =>
      entry.models.map((m) => [m.id, m.repo ?? m.id] as const),
    ),
  );

  // Repo id -> the curated `label` (AI-2c, catalog.py): a human display name is
  // a CURATED field, never one derived by mechanically stripping a repo id at
  // runtime — `modelName()` (RepoCard.tsx) is exactly that mechanical strip,
  // kept only as the fallback for a repo the curation does not name. Keyed by
  // the same `m.repo ?? m.id` as `repoById` above, since a card's `repo.id` is
  // the disk-side address, not the curated id a llama.cpp GGUF is filed under.
  const labelByRepoId = new Map<string, string>(
    (catalog ?? []).flatMap((entry) =>
      entry.models.map((m) => [m.repo ?? m.id, m.label] as const),
    ),
  );

  // The click is held until something ELSE can speak for the pull — the runtime
  // reporting it, `settling` carrying it through the walk, or the walk finding
  // it on disk (a "download" that was a cache hit and finished before any poll
  // saw it). Clearing on the POST's reply would put the card back to "Download"
  // for the beat before the next runtime poll, which reads as the button having
  // done nothing.
  //
  // **That third arm is the ONLY one a cache hit ever reaches, so it is the one
  // that has to speak the disk's language.** `downloading`/`settling` are keyed
  // by model id, but `onCard` is keyed by repo id, and asking it about a bare
  // GGUF filename could never be true — a Download pressed on a model already
  // fully cached had no arm that could settle it, and the click stayed held
  // until the next thing re-rendered the tab.
  const spokenFor =
    starting !== null &&
    (downloading.has(starting) ||
      settling.has(starting) ||
      !!onCard?.has(repoById.get(starting) ?? starting));
  useEffect(() => {
    if (spokenFor) setStarting(null);
  }, [spokenFor]);

  const runDownload = async (model: string, capability: string) => {
    setRuntimeError(null);
    setStarting(model);
    try {
      await downloadAiModel(model, capability);
      // The pull is the server's now. Asking the runtime for a fresh read is the
      // whole follow-up: the card's state comes from what is actually
      // happening, never from the fact that a button was pressed.
      refreshAiRuntime();
    } catch (e) {
      setRuntimeError((e as Error).message);
      setStarting(null);
    }
  };

  // The download manager's ✕, on the card that started the download. It is a
  // REQUEST — the row stays until the worker honours it — so nothing is patched
  // here beyond dropping the held click: the next jobs tick (one second, and the
  // poll is running because the runtime is busy) brings "Cancelling…" from the
  // job row itself rather than from a local guess about it.
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
      // The endpoint answers with the fresh listing, so a delete costs no
      // second walk — published straight into the page's scan state.
      publishListing(result);
      setFailures(
        result.failures.map(
          (f) =>
            `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`,
        ),
      );
      // A deletion that freed nothing is worth saying out loud too — it means
      // every target failed, and the banner beside it says why.
      pushToast({
        msg: result.freed
          ? `Freed ${formatSize(result.freed)} — ${label}`
          : `Nothing deleted — ${label}`,
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

  // Which capability the curation files each model under — the fallback a partly
  // downloaded repo's resume needs, since the capability read off half a snapshot
  // is often null. Built from the catalog's own shape (a capability entry owning
  // its models) rather than from a field on the model row, which does not exist
  // and should not: the entry IS the answer.
  const capabilityById = new Map<string, string>(
    (catalog ?? []).flatMap((entry) =>
      entry.models.map((m) => [m.id, entry.capability] as const),
    ),
  );

  // Which repo ids the curation actually names — the tick beside a card's name.
  // Keyed by `m.repo ?? m.id` like the two maps around it, and deliberately the
  // same equality the merge uses to drop a recommendation for a model already on
  // disk: the tick and that de-duplication have to agree, or a card could wear
  // no tick while a second card recommending it sat in the same section.
  const curated = curatedRepoIds(catalog);

  // What the curation says a model's whole download WEIGHS, in bytes — the
  // denominator a partly downloaded card paints its fraction against when no
  // live job is reporting real byte counts (`partialFraction`).
  //
  // Two approximations are baked in and both are acceptable for a wash rather
  // than for a figure: `size_gb` is a curated round number, and for a model
  // whose download touches several repos it covers ALL of them (catalog.py), so
  // one repo's share reads low. Binary GB to match `formatSize`, which is what
  // the same card prints beside it.
  const estimateById = new Map<string, number>(
    (catalog ?? []).flatMap((entry) =>
      entry.models.flatMap((m) =>
        m.size_gb ? [[m.id, m.size_gb * 1024 ** 3] as const] : [],
      ),
    ),
  );

  // Derived on every render rather than memoised: it is one pass over a list
  // whose length is the number of repos in a cache, and `repos` is a fresh array
  // each render anyway — a memo keyed on it would recompute every time and cost
  // the comparison on top.
  const grouped = groupRepos(repos);
  // …and then the two payloads joined into the rows the page draws: disk cards
  // first (loaded ones leading), then what the curation recommends for the same
  // capability. The rule is in `aiModelGroups` with the bucketing, because which
  // rows a capability has and in what order is exactly the kind of thing that is
  // wrong invisibly — a recommended card for a model already on disk is one
  // model appearing twice, and it looks like two models.
  const sections = mergeSections(grouped.models.groups, catalog, loadedById, onCard);
  // Which engine serves each capability here. The sections take theirs from
  // `mergeSections`, which reads this same table — a Hub result has no section to
  // belong to, only a `capability`, so it asks the table directly.
  const runners = runnersByCapability(catalog);

  // Which face of the page is on screen. From the SETTLED query, so the layout
  // does not swap on the first keystroke; the ✕ in the box is the one piece of
  // chrome that follows the LIVE controls instead, because it belongs to the box
  // rather than to the grid.
  const face = searchChrome(settled.q, settled.task).face;
  const live = searchChrome(query, task);

  /** The three-way guard, asked once: a pull is live if the runtime reports it,
   *  if this tab just clicked it, or if it has stopped being reported and the
   *  confirming walk has not landed. Both faces of the page ask it — a download
   *  started from the search results is the same pull as one started from a
   *  carousel, and two spellings of "busy" would be two answers about it. */
  const pulling = (id: string) =>
    downloading.has(id) || starting === id || settling.has(id);

  // One card, wherever it ends up. Written once because a section is only a
  // heading and a subset — nothing about a card changes with the group it is
  // drawn in, and two copies of this call site would be two places for a prop
  // to go missing.
  const card = (r: AiModelRepo) => {
    // What a RESUME would be filed under, for a partly downloaded repo. Its own
    // `capability` first — read off whatever landed, and right when it is there
    // — then the curation, which knows every model this tab's own Download
    // button ever started a pull for. Null for a repo neither can place, and the
    // card says so on a disabled button rather than dropping the control.
    //
    // **`unservable` overrules both, and it has to be checked FIRST.** A partly
    // downloaded repo has no weights, so `capabilityById` correctly omits a
    // Mac-only model on Linux — but `r.capability` is read off the config that
    // DID land and happily says "embeddings", which put the resume back and sent
    // the download into a runner that cannot read the format. The server is what
    // knows no engine here can serve it (`hub_cache._engine` ->
    // `catalog.engine_gap`), and this is the same answer the load and download
    // routes refuse on — so the card cannot offer what the route would reject.
    const resumeCapability = r.engine?.unservable
      ? null
      : r.capability ?? capabilityById.get(r.id) ?? null;
    return (
      <RepoCard
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
        estimate={estimateById.get(r.id) ?? null}
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
      {/* At the TOP, above everything, because it is the one control that
          changes what the whole page is. */}
      <SearchControls
        query={query}
        task={task}
        sort={sort}
        includeUnfit={includeUnfit}
        showsReset={live.showsReset}
        searchBox={searchBox}
        onQuery={setQuery}
        onTask={setTask}
        onSort={setSort}
        onIncludeUnfit={setIncludeUnfit}
        onClear={clearSearch}
      />
      {load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
      {runtimeError && <ErrorBanner>{runtimeError}</ErrorBanner>}
      {failures.length > 0 && (
        <ErrorBanner>
          {failures.map((f) => (
            <div key={f}>{f}</div>
          ))}
        </ErrorBanner>
      )}
      {/* ONE face at a time. A query replaces this machine's own models with the
          Hub's answer; clearing the box brings them back, which is what makes
          the box safe to type in. The delete dialogs below stay mounted through
          both — a confirmation is about a repo, not about a view. */}
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
          {load.status === "loading" && (
            <p className="cc-empty">Reading the Hugging Face cache…</p>
          )}
          {data &&
            (sections.length || grouped.components.repos.length ? (
              <>
                {/* Section A. One row per capability: what somebody downloaded for
                    it, then what the curation says to get. Rendered at all only
                    when there is one — a machine holding nothing but a runner's
                    own components, whose catalog recommends nothing, should not be
                    told it has a Models section. */}
                {sections.length > 0 && (
                  <section className="am-section">
                    {/* "User downloaded models", not "Models". The page is
                        titled AI Models and every card on it is one, so the bare
                        word restated the page; what actually distinguishes this
                        section from the one below it is WHO asked for these —
                        which is the same distinction "Fetched by engines"
                        already draws from the other side. Still true of a row
                        that ends in recommendations: those are models for the
                        user to choose, which is the same side of the line. */}
                    <SectionHead title="User downloaded models" />
                    {sections.map((section) => (
                      <div className="am-subgroup" key={section.key}>
                        <div className="am-subgroup-head">
                          <h4 className="am-subgroup-title">{section.label}</h4>
                          {/* DISK bytes, and only disk bytes — the figure beside a
                              heading is a claim about this machine, and counting
                              models nobody has downloaded would make it the one
                              number on the page that cannot be checked against
                              the cache (see `MergedSection.size`). A row that is
                              all recommendations has nothing to state here. */}
                          {section.size > 0 && (
                            <span className="am-subgroup-size">{formatSize(section.size)}</span>
                          )}
                        </div>
                        {section.note && <p className="am-group-note">{section.note}</p>}
                        <Carousel>
                          {section.disk.map(card)}
                          {section.recommended.map((m) => (
                            <RecommendedCard
                              key={m.id}
                              model={m}
                              runner={section.runner}
                              busy={pulling(m.id)}
                              job={jobByModel.get(m.id)}
                              onDownload={() => runDownload(m.id, section.key)}
                              onCancel={runCancelDownload}
                            />
                          ))}
                        </Carousel>
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
                    <SectionHead title="Fetched by engines" />
                    {/* One sentence. The three it replaced said WHY these are
                        listed at all, which the heading and the "part of X" tag
                        on every card already answer — what only prose can carry
                        is that deleting one is safe, and that is what is left. */}
                    <p className="am-group-note">
                      Automatically downloaded by a runner. If deleted, it will be downloaded
                      again on next run.
                    </p>
                    {/* Same carousel, disk rows only: nothing recommends a
                        component, because nobody chooses one. */}
                    <Carousel>{grouped.components.repos.map(card)}</Carousel>
                  </section>
                )}
              </>
            ) : catalog === null ? (
              // The catalog has not answered, and on an empty cache it is the only
              // thing that can put a card on this page. Saying "nothing here" first
              // and filling the page a moment later would report a fact this page
              // had not finished checking.
              <p className="cc-empty">Reading the model catalog…</p>
            ) : (
              // Two different nothings: no cache dir at all (nothing has ever
              // pulled from the Hub) versus a cache that has been emptied. The
              // path itself is already in the caption above, so it isn't repeated
              // here.
              //
              // It used to carry a button to the Discover tab, because either
              // nothing ended in the same next move and the tab strip was too far
              // from the middle of the page to be an instruction (HF-8, D265).
              // The move is on this tab now: a machine with no models still gets a
              // row per capability with what to download in it, so this state is
              // only reachable when the CURATION is empty too — a catalog that
              // could not be read, or one with no runner on this platform. There
              // is nowhere to send anybody from here, and a button that went to a
              // second empty grid was never the answer. The search box above is
              // still a way out of it, and it is on screen in this state too.
              <p className="cc-empty">
                {data.exists
                  ? "Nothing cached here yet."
                  : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
              </p>
            ))}
        </>
      )}
      <DeleteDialogs
        pending={pending}
        busy={busy}
        onClose={() => setPending(null)}
        onConfirm={runDelete}
      />
    </>
  );
}
