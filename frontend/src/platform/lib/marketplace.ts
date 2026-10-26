// The Marketplace's data layer (fused_render/marketplace.py, the
// /api/marketplace/* routes): typed fetchers for the catalog, one app's
// detail, the install jobs and the install POST, plus the two small stores
// the pages share.
//
// CATALOG — a module cache, not a per-mount fetch. Every marketplace route
// remounts on the nav epoch (App.tsx), so a card → detail → Back hop would
// otherwise paint skeletons twice for a list that has not changed. The cache
// answers instantly and a background revalidate (single-flight) lands any
// change; `refreshCatalog({force: true})` asks the server to re-read the udf.ai catalog.
//
// JOBS — polled every ~600ms ONLY while a job is active (queued /
// downloading / installing). The poll stops on its own once everything has
// settled, and is started again by `installApp` (or by a catalog read that
// finds a job already running — another window's install). When a job lands
// on `done`, the catalog is re-read so the card flips to Open with the
// server's `installed` stamp, and a "<App> installed — Open" notification is
// raised for installs THIS window started.
//
// Lives in platform (not apps/marketplace) because the phase-2 `.fused` file
// install page (/marketplace/file?path=…) is a second consumer of the same
// jobs + install plumbing.
import { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { encodeFsPathSegments, navigateUrl, urlForFsPath, viewUrlForFsPath } from "@platform/lib/router";

// -- Wire types (the API contract) -------------------------------------------

export interface MarketplaceAuthor {
  name: string;
  github?: string | null;
}

/** A block-list entry as the Marketplace reports it on an installed copy
 *  (CONTRIB_LOOP.md §18.2/§18.4). `hard`: the app is stopped and rolled back;
 *  `soft`: updates pause and opening asks first. */
export interface MarketBlock {
  level: "hard" | "soft";
  reason: string;
  what_to_do: string;
  at?: string | null;
  commit?: string | null;
  /** The release the copy went back to (hard), when there was one. */
  rolled_back_to?: string | null;
  /** That release's date, in words ("Oct 26") — `blocks.view`. */
  rolled_back_date?: string | null;
  /** …or as ISO, from a server that sends that instead. */
  rolled_back_to_at?: string | null;
  no_target?: boolean;
  confirmed?: boolean;
  confirmed_at?: number | null;
  conflict?: boolean;
  /** The app folder (the open gate's 423 carries it). */
  path?: string | null;
  slug?: string | null;
}

/** What is on this machine for a catalog row (§18.5). Stage B: `kind`
 *  `marketplace` is a copy of the author's repo pinned to a reviewed release;
 *  `legacy` is a plain-file install from the old package Marketplace, which
 *  never updates and offers the switch instead. An older server's stamp has
 *  no `kind` and reads as before. */
export interface InstalledStamp {
  path: string;
  /** `own`: the author's own clone of the listed repo — "Your app"; it
   *  never updates or switches from here, it only opens. */
  kind?: "marketplace" | "legacy" | "own";
  /** The release the copy runs (marketplace). */
  commit?: string | null;
  /** A newer release is waiting; the next check brings it in (marketplace). */
  update_available?: boolean;
  /** Null when the stamp predates a version (or could not be read). */
  version?: string | null;
  /** The release the copy was installed from (legacy stamps). */
  release_id?: string | null;
  installed_at?: string;
  /** Local files differ from the sha256s stamped at install time (legacy). */
  modified?: boolean;
  /** The row is gone from the index: the copy stays on its last release. */
  delisted?: boolean;
  block?: MarketBlock | null;
}

/** One release of a row, newest first: the review's What's new, and whether
 *  Fused blocked it since. */
export interface MarketRelease {
  commit: string;
  at: string | null;
  whats_new: string | null;
  version: string | null;
  blocked: "hard" | "soft" | null;
}

export interface AppEntry {
  slug: string;
  name: string;
  description: string;
  author: MarketplaceAuthor;
  tags: string[];
  category: string;
  version: string;
  requires_python: boolean;
  min_fused_render: string | null;
  ai_capabilities: string[];
  ai_models: string[];
  ai_model_param: string | null;
  icon_url: string | null;
  preview_url: string | null;
  size_bytes: number;
  file_count: number;
  release_id: string;
  commit: string;
  published_at: string | null;
  updated_at: string | null;
  source_url: string | null;
  featured: boolean;
  installed: InstalledStamp | null;
  update_available: boolean;
  compatible: boolean;
  // ---- stage B (§18.3/§18.5); optional so an older server still types ----
  /** "alice/sine-starter": the author's repo the release is pinned in. */
  repo?: string | null;
  branch?: string | null;
  /** The slug of the listed app this one remixes. */
  remix_of?: string | null;
  remix_count?: number;
  /** Distinct people credited across its releases, the author included. */
  contributor_count?: number;
  /** ISO date of the newest release. */
  last_release_at?: string | null;
  releases?: MarketRelease[];
  /** The newest release's What's new (= releases[0].whats_new). */
  whats_new?: string | null;
}

export interface MarketplaceCategory {
  id: string;
  label: string;
  count: number;
}

/** A server-curated group of apps for the home page. `shelf` groups map to a
 *  category (`id` is its id); `new_and_updated` has no category. */
export interface MarketplaceCollection {
  id: string;
  kind: "shelf" | "new_and_updated";
  title: string;
  slugs: string[];
}

export interface Catalog {
  status: "ok" | "error";
  message?: string;
  error?: string;
  source?: "udf-api";
  api?: string;
  catalog_version?: string;
  /** The index repo the catalog is read from ("owner/name"), for the footer. */
  index?: string | null;
  generated_at?: string;
  fetched_at?: string;
  stale?: boolean;
  warning?: string | null;
  categories: MarketplaceCategory[];
  collections: MarketplaceCollection[];
  apps: AppEntry[];
}

export interface AppRelease {
  release_id: string;
  version: string;
  changelog?: string | null;
  published_at?: string | null;
  revoked: boolean;
}

export interface SearchHit extends AppEntry {
  score?: number;
  highlights?: { field: string; snippet: string }[];
}

export interface SearchResult {
  status: "ok" | "error";
  message?: string;
  apps: SearchHit[];
  next_cursor: string | null;
}

export interface AppFile {
  path: string;
  size: number;
}

export interface AppDetail {
  status: "ok";
  app: AppEntry;
  readme: string;
  files: AppFile[];
  readme_base: string;
  /** Stage B: the row's releases (§18.3 `marketplace_app_detail`); an older
   *  server sent `AppRelease`s, which nothing reads any more. */
  releases?: MarketRelease[] | AppRelease[];
}

export type JobState = "queued" | "downloading" | "installing" | "done" | "error";

export interface InstallJob {
  slug: string;
  action: "install" | "update";
  state: JobState;
  progress: number;
  bytes_done: number;
  bytes_total: number;
  message: string;
  path?: string;
  error?: string;
}

// -- Fetchers ----------------------------------------------------------------

export function fetchCatalog(refresh = false): Promise<Catalog> {
  return getJson<Catalog>(`/api/marketplace/catalog${refresh ? "?refresh=1" : ""}`);
}

export function searchApps(opts: { q: string; category?: string; cursor?: string | null; limit?: number }): Promise<SearchResult> {
  const p = new URLSearchParams({ q: opts.q });
  if (opts.category && opts.category !== "all") p.set("category", opts.category);
  if (opts.cursor) p.set("cursor", opts.cursor);
  if (opts.limit) p.set("limit", String(opts.limit));
  return getJson<SearchResult>(`/api/marketplace/search?${p}`);
}

export function fetchApp(slug: string): Promise<AppDetail> {
  return getJson<AppDetail>(`/api/marketplace/app/${encodeURIComponent(slug)}`);
}

export function fetchJobs(): Promise<{ jobs: Record<string, InstallJob> }> {
  return getJson<{ jobs: Record<string, InstallJob> }>("/api/marketplace/jobs");
}

export function fetchInstalled(): Promise<{ apps: AppEntry[] }> {
  return getJson<{ apps: AppEntry[] }>("/api/marketplace/installed");
}

/** The install POST's answers: a job, or (§18.4 D13) a question — the user
 *  already follows this app's author by GitHub link, and installing from the
 *  Marketplace would switch that copy to reviewed releases. Posting again
 *  with `switch: true` is the yes. */
export type InstallAnswer =
  | { status: "ok"; job: InstallJob }
  | { status: "confirm"; switch: "reviewed"; path: string; who: string | null };

export function postInstall(slug: string, opts: { switch?: boolean } = {}): Promise<InstallAnswer> {
  return postJson<InstallAnswer>("/api/marketplace/install", { slug, ...(opts.switch ? { switch: true } : {}) });
}

/** Stage B's answer shape for the two Installed-page buttons. */
export interface MarketAnswer {
  status: "ok" | "error";
  message?: string;
  error?: string;
  path?: string;
  /** /check: the copy's sync (`git_upstream.sync_repo`'s answer), null
   *  when there was none; `app` the row as it reads after the check. */
  sync?: { ok?: boolean; message?: string } | null;
  app?: AppEntry | null;
}

/** "Check for a new release" (§18.5): a fresh catalog + block list read and a
 *  sync of that copy — the way to move now instead of on the next check. */
export function postCheck(slug: string): Promise<MarketAnswer> {
  return postJson<MarketAnswer>("/api/marketplace/check", { slug });
}

/** "Switch to the reviewed version" on a plain-file install from the old
 *  Marketplace (§18.4 `adopt_legacy`): the copy becomes a reviewed copy in
 *  place, edits and data kept. */
export function postAdopt(slug: string): Promise<MarketAnswer> {
  return postJson<MarketAnswer>("/api/marketplace/adopt", { slug });
}

// -- Server search -----------------------------------------------------------

export interface MarketplaceSearch {
  status: "idle" | "loading" | "ok" | "error";
  apps: SearchHit[];
  nextCursor: string | null;
  /** A load-more request is in flight (the button disables meanwhile). */
  loadingMore: boolean;
  loadMore: () => void;
}

export const SEARCH_DEBOUNCE_MS = 200;

/** Server-side search for a typed query: 200 ms debounce, a sequence id so a
 *  slow earlier answer never overwrites a later one, and previous results stay
 *  while the next load. `idle` when `q` is blank. An error leaves the caller to
 *  fall back to its own local filter.
 *
 *  A cursor belongs to ONE (term, category): a change drops it at once (Load
 *  more disappears while the new query debounces and loads), and a load-more
 *  answer that left before the change is discarded. Only one load-more runs at
 *  a time, and appended apps are de-duplicated by slug. */
export function useMarketplaceSearch(q: string, category = "all"): MarketplaceSearch {
  const [state, setState] = useState<{
    status: MarketplaceSearch["status"];
    apps: SearchHit[];
    nextCursor: string | null;
    loadingMore: boolean;
  }>({
    status: "idle",
    apps: [],
    nextCursor: null,
    loadingMore: false,
  });
  const seq = useRef(0);
  const cursor = useRef<string | null>(null);
  const loadingMore = useRef(false);
  const term = q.trim();
  const key = `${category}\u0000${term}`;
  const keyRef = useRef(key);
  keyRef.current = key;

  useEffect(() => {
    const id = ++seq.current;
    cursor.current = null;
    loadingMore.current = false;
    if (!term) {
      setState({ status: "idle", apps: [], nextCursor: null, loadingMore: false });
      return;
    }
    setState((s) => ({ ...s, status: "loading", nextCursor: null, loadingMore: false }));
    const t = setTimeout(() => {
      searchApps({ q: term, category })
        .then((r) => {
          if (id !== seq.current) return;
          if (r.status !== "ok") return setState({ status: "error", apps: [], nextCursor: null, loadingMore: false });
          cursor.current = r.next_cursor;
          setState({ status: "ok", apps: r.apps, nextCursor: r.next_cursor, loadingMore: false });
        })
        .catch(() => {
          if (id === seq.current) setState({ status: "error", apps: [], nextCursor: null, loadingMore: false });
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [term, category]);

  const loadMore = () => {
    const next = cursor.current;
    if (!next || !term || loadingMore.current) return;
    const id = seq.current;
    const issuedFor = key;
    loadingMore.current = true;
    setState((s) => ({ ...s, loadingMore: true }));
    const stale = () => id !== seq.current || issuedFor !== keyRef.current;
    searchApps({ q: term, category, cursor: next })
      .then((r) => {
        if (stale() || r.status !== "ok") return;
        cursor.current = r.next_cursor;
        setState((s) => {
          const have = new Set(s.apps.map((a) => a.slug));
          return {
            status: "ok",
            apps: s.apps.concat(r.apps.filter((a) => !have.has(a.slug))),
            nextCursor: r.next_cursor,
            loadingMore: s.loadingMore,
          };
        });
      })
      .catch(() => {})
      .finally(() => {
        if (stale()) return; // the query change already reset the flag
        loadingMore.current = false;
        setState((s) => (s.loadingMore ? { ...s, loadingMore: false } : s));
      });
  };

  return { ...state, loadMore };
}

// -- Small helpers -----------------------------------------------------------

export function isActive(job: InstallJob | null | undefined): boolean {
  return !!job && (job.state === "queued" || job.state === "downloading" || job.state === "installing");
}

/** Category display order (the server already orders `categories`; this is
 *  the fallback label for an id a stale catalog does not list). */
export function categoryLabel(catalog: Catalog | null, id: string): string {
  const hit = catalog?.categories.find((c) => c.id === id);
  if (hit) return hit.label;
  return id
    .split("-")
    .map((w) => (w === "ai" ? "AI" : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/** Where "Open" lands for an installed app: its ENTRY PAGE in the explorer's
 *  file view — the same destination an /apps card opens (appEntry.ts,
 *  D269: a card opens the app's top-level page, not its folder). Every
 *  community app ships an `index.html` at its root (the repo's contract), so
 *  the entry is known without an /api/apps round-trip. */
export function installedAppHref(dir: string): string {
  return urlForFsPath(dir.replace(/\/+$/, "") + "/index.html");
}

export function openInstalledApp(dir: string): void {
  navigateUrl(installedAppHref(dir), { isDir: false });
}

/** The app's own page (/apps/<folder>, shell/AppPage.tsx) — Overview /
 *  Tasks / Files for that folder. Same encoding UninstallAppModal uses. */
export function appPageHref(dir: string): string {
  return "/apps/" + encodeFsPathSegments(dir);
}

/** "Show folder": the explorer's listing of the install directory. */
export function showInstalledFolder(dir: string): void {
  navigateUrl(viewUrlForFsPath(dir), { isDir: true });
}

export const MARKETPLACE_HOME = "/marketplace";

export function appDetailHref(slug: string): string {
  return `/marketplace/app/${encodeURIComponent(slug)}`;
}

/** The Marketplace backend's base (the `fused_app_marketplace` canvas,
 *  §18.3) — mirrors fused_render/marketplace.py `API_BASE`, which the
 *  server reads per call (FUSED_RENDER_MARKETPLACE_API overrides it there,
 *  not here). Only the public listing page link is built from it. */
export const MARKETPLACE_API_BASE = "https://unstable.udf.ai/fc_5OHf9uj6AGOKqLRH25kkFU";

/** A listed app's public listing page (its README, releases, reviews). */
export function listingPageUrl(slug: string): string {
  return `${MARKETPLACE_API_BASE}/fused_app_listing?slug=${encodeURIComponent(slug)}`;
}

// -- Catalog store -----------------------------------------------------------

export interface CatalogSnapshot {
  catalog: Catalog | null;
  error: string | null;
  loading: boolean;
}

let snapshot: CatalogSnapshot = { catalog: null, error: null, loading: false };
let catalogFlight: Promise<void> | null = null;
const catalogListeners = new Set<(s: CatalogSnapshot) => void>();

function setSnapshot(next: Partial<CatalogSnapshot>) {
  snapshot = { ...snapshot, ...next };
  for (const l of catalogListeners) l(snapshot);
}

/** Monotonic id of the latest catalog read: an `again`/`force` read can
 *  overtake one already in flight, and the older answer — which may predate
 *  an install — must never land on top of the newer one. */
let catalogSeq = 0;

/** Re-read the catalog. Single-flight unless `force` (a "Retry"/refresh
 *  press, which also asks the server to bypass its 10-minute cache) or
 *  `again` (something just changed on disk — a read already in flight may
 *  have been answered before the change, so joining it would miss it). */
export function refreshCatalog(opts: { force?: boolean; again?: boolean } = {}): Promise<void> {
  if (catalogFlight && !opts.force && !opts.again) return catalogFlight;
  const seq = ++catalogSeq;
  setSnapshot({ loading: true });
  // Finished jobs this read will reconcile: whatever had settled BEFORE it
  // left is reflected in its answer (see `reconciled` below).
  const settling = [...awaiting.keys()];
  const flight = fetchCatalog(!!opts.force)
    .then((c) => {
      // Superseded: a newer read is in flight (or already landed) and owns
      // the snapshot — and the `awaiting` slugs this one would have settled,
      // since they are still pending when that one left.
      if (seq !== catalogSeq) return;
      if (c.status === "error") {
        // Total failure (nothing cached server-side either): `apps` is empty.
        // Keep whatever is already on screen and say why the read failed; with
        // nothing on screen, the page shows its error state.
        setSnapshot({ error: c.message || c.error || "Couldn't load the catalog", loading: false });
        retryAwaiting();
      } else {
        for (const slug of settling) {
          awaiting.delete(slug);
          reconciled.add(slug);
        }
        setSnapshot({ catalog: c, error: null, loading: false });
        emitJobs();
      }
    })
    .catch((e: Error) => {
      if (seq !== catalogSeq) return;
      setSnapshot({ error: e.message || "Couldn't reach the server", loading: false });
      retryAwaiting();
    })
    .finally(() => {
      if (catalogFlight === flight) catalogFlight = null;
    });
  catalogFlight = flight;
  // A catalog read is also the cheapest moment to notice an install that is
  // already running (another window started it): one jobs read, which starts
  // the poll only if something is active.
  void pollJobsOnce();
  return flight;
}

/** The re-read after a job finished failed, so nothing on screen has the
 *  new `installed` stamp yet and the finished jobs stay `awaiting` (read as
 *  Open). Try again a few seconds later; past AWAIT_MAX_MS (the server
 *  forgets a finished job after ~60s anyway) give up on them — the next
 *  successful read, whenever it comes, is the truth. */
const AWAIT_RETRY_MS = 4000;
const AWAIT_MAX_MS = 60_000;
let awaitRetry: ReturnType<typeof setTimeout> | null = null;
function retryAwaiting() {
  if (awaitRetry || awaiting.size === 0) return;
  awaitRetry = setTimeout(() => {
    awaitRetry = null;
    const now = Date.now();
    let dropped = false;
    for (const [slug, since] of awaiting) {
      if (now - since >= AWAIT_MAX_MS) {
        awaiting.delete(slug);
        reconciled.add(slug);
        dropped = true;
      }
    }
    if (dropped) emitJobs();
    if (awaiting.size > 0) void refreshCatalog({ again: true });
  }, AWAIT_RETRY_MS);
}

/** The current catalog snapshot, outside React (tests, one-off reads). */
export function peekCatalog(): CatalogSnapshot {
  return snapshot;
}

/** The catalog, from the module cache when there is one (instant on a Back),
 *  revalidated in the background on every mount. */
/** `read` false: subscribe to what is cached but never ask the server — a
 *  `.fused` install page shown with the store off (MarketplacePage `bare`). */
export function useCatalog(read = true): CatalogSnapshot {
  const [s, setS] = useState(snapshot);
  useEffect(() => {
    catalogListeners.add(setS);
    setS(snapshot);
    if (read) void refreshCatalog();
    return () => {
      catalogListeners.delete(setS);
    };
  }, [read]);
  return s;
}

/** Patch one entry in the cached catalog (a detail fetch is fresher). */
function patchCatalogApp(app: AppEntry) {
  const c = snapshot.catalog;
  if (!c) return;
  const i = c.apps.findIndex((a) => a.slug === app.slug);
  if (i < 0) return;
  const apps = c.apps.slice();
  apps[i] = app;
  setSnapshot({ catalog: { ...c, apps } });
}

// UNINSTALL reuses the shell's own flow (openUninstallApp → UninstallAppModal),
// which knows nothing about the Marketplace — but it announces every removal
// with the current-apps wall-throw (tasksChanged.ts). That is the catalog's cue:
// the folder is gone, so the server stops reporting `installed`. Module-level
// and once, so it holds whichever marketplace page is mounted (or none — the
// next mount then starts from a correct cache).
if (typeof window !== "undefined") {
  window.addEventListener(CURRENT_APPS_CHANGED_EVENT, () => {
    if (snapshot.catalog) void refreshCatalog({ again: true });
  });
}

// -- App detail (small per-slug cache, same instant-Back reasoning) -----------

const detailCache = new Map<string, AppDetail>();

export interface DetailSnapshot {
  detail: AppDetail | null;
  error: string | null;
}

export function useAppDetail(slug: string): DetailSnapshot & { reload: () => void } {
  const [state, setState] = useState<DetailSnapshot>(() => ({
    detail: detailCache.get(slug) ?? null,
    error: null,
  }));
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    fetchApp(slug)
      .then((d) => {
        if (!live) return;
        detailCache.set(slug, d);
        patchCatalogApp(d.app);
        setState({ detail: d, error: null });
      })
      .catch((e: Error) => {
        if (live) setState((s) => ({ detail: s.detail, error: e.message || "Couldn't load this app" }));
      });
    return () => {
      live = false;
    };
  }, [slug, tick]);
  // The catalog store is the one that hears about installs: when the cached
  // entry for this slug changes (install done, uninstalled), mirror it into
  // the detail so the hero's action follows without a refetch.
  useEffect(() => {
    const onCatalog = (s: CatalogSnapshot) => {
      const fresh = s.catalog?.apps.find((a) => a.slug === slug);
      if (!fresh) return;
      setState((cur) => {
        if (!cur.detail || cur.detail.app === fresh) return cur;
        const detail = { ...cur.detail, app: fresh };
        detailCache.set(slug, detail);
        return { ...cur, detail };
      });
    };
    catalogListeners.add(onCatalog);
    return () => {
      catalogListeners.delete(onCatalog);
    };
  }, [slug]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}

// -- Jobs store ----------------------------------------------------------------

const POLL_MS = 600;
let jobs: Record<string, InstallJob> = {};
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let polling = false;
/** Bumped whenever this window merges a job of its own (installApp). A poll
 *  that left before the bump answers for an older world — its map lacks the
 *  job just started — so its answer is dropped instead of wiping the new
 *  job back to Get; the next tick reads the server again. */
let jobsGen = 0;
const jobListeners = new Set<(j: Record<string, InstallJob>) => void>();
/** Slugs whose install THIS window started — the ones that earn a toast. */
const startedHere = new Map<string, string>();
/** Slugs already toasted for the job now listed, so a finished job that stays
 *  listed for ~60s is not announced on every poll. */
const announced = new Set<string>();

/** Done jobs whose result the catalog does not show yet (the re-read is in
 *  flight), with when they finished: the pill reads them as Open, so it
 *  never flashes back to Get — or, for an update, to Update — in the gap. */
const awaiting = new Map<string, number>();
/** Done jobs the catalog HAS caught up with. The server keeps a finished job
 *  listed for ~60s; past reconciliation it says nothing the catalog does not,
 *  and holding on to it would be wrong the moment the app is uninstalled
 *  (the stale "done" would keep reading as installed). Hidden from readers. */
const reconciled = new Set<string>();

/** The jobs readers see: every job, minus finished ones already reflected
 *  in the catalog. */
function visibleJobs(): Record<string, InstallJob> {
  if (reconciled.size === 0) return jobs;
  const out: Record<string, InstallJob> = {};
  for (const [slug, job] of Object.entries(jobs)) {
    if (job.state === "done" && reconciled.has(slug)) continue;
    out[slug] = job;
  }
  return out;
}

let visible: Record<string, InstallJob> = {};
function emitJobs() {
  visible = visibleJobs();
  for (const l of jobListeners) l(visible);
}

function setJobs(next: Record<string, InstallJob>) {
  const prev = jobs;
  jobs = next;
  let settled = false;
  for (const [slug, job] of Object.entries(next)) {
    const before = prev[slug];
    if (job.state === "done" && (!before || before.state !== "done")) {
      settled = true;
      reconciled.delete(slug);
      awaiting.set(slug, Date.now());
      announceDone(job);
    } else if (job.state === "error" && (!before || before.state !== "error")) {
      settled = true;
    } else if (isActive(job)) {
      reconciled.delete(slug);
    }
  }
  emitJobs();
  // The card's `installed` stamp comes from the catalog, so a job that just
  // settled is the moment to re-read it (and the cached detail follows).
  if (settled) void refreshCatalog({ again: true });
}

function announceDone(job: InstallJob) {
  const name = startedHere.get(job.slug);
  if (!name || announced.has(job.slug)) return;
  announced.add(job.slug);
  startedHere.delete(job.slug);
  const path = job.path;
  notify({
    title: `${name} ${job.action === "update" ? "updated" : "installed"}`,
    tone: "info",
    action: path ? { label: "Open", onClick: () => openInstalledApp(path) } : undefined,
  });
}

function anyActive(): boolean {
  return Object.values(jobs).some(isActive);
}

function schedule() {
  if (pollTimer || !anyActive()) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    void pollJobsOnce();
  }, POLL_MS);
}

async function pollJobsOnce(): Promise<void> {
  if (polling) return;
  polling = true;
  const departed = jobsGen;
  try {
    const r = await fetchJobs();
    if (departed === jobsGen) setJobs(r.jobs ?? {});
  } catch {
    /* A missed poll is retried by the next tick while anything is active. */
  } finally {
    polling = false;
    schedule();
  }
}

/** Start an install (or an update — the server decides from what is on
 *  disk). The returned job is merged in at once so the pill turns into a
 *  ring on the same frame, then the poll takes over. A `confirm` answer (a
 *  GitHub-link copy of this app exists: switch it to reviewed releases?)
 *  starts nothing and is handed back for the caller's sheet; the same call
 *  with `switch: true` is the yes. */
export async function installApp(
  app: { slug: string; name: string },
  opts: { switch?: boolean } = {},
): Promise<InstallJob | Extract<InstallAnswer, { status: "confirm" }>> {
  startedHere.set(app.slug, app.name);
  announced.delete(app.slug);
  try {
    const r = await postInstall(app.slug, opts);
    if (r.status === "confirm") {
      startedHere.delete(app.slug);
      return r;
    }
    jobsGen += 1;
    setJobs({ ...jobs, [app.slug]: r.job });
    schedule();
    return r.job;
  } catch (e) {
    startedHere.delete(app.slug);
    const message = (e as Error).message || "Install failed";
    jobsGen += 1;
    setJobs({
      ...jobs,
      [app.slug]: {
        slug: app.slug,
        action: "install",
        state: "error",
        progress: 0,
        bytes_done: 0,
        bytes_total: 0,
        message,
        error: message,
      },
    });
    throw e;
  }
}

// -- Release checks (stage B) ---------------------------------------------------
//
// A reviewed copy's Update is "Check for a new release" (POST
// /api/marketplace/check): the server re-reads the catalog and the block
// list and syncs that copy, which brings a waiting release in (with its
// Activity line and Undo on the app page). Not a job — one request — so the
// pill's ring reads this small set while it is in flight.

const checking = new Set<string>();
const checkListeners = new Set<() => void>();
function emitChecking() {
  for (const l of checkListeners) l();
}

/** Check one reviewed copy for a new release now. Resolves with the server's
 *  answer (a refusal included); the catalog is re-read either way so the
 *  pill and the Installed row follow. */
export async function checkForRelease(app: { slug: string }): Promise<MarketAnswer> {
  if (checking.has(app.slug)) return { status: "ok" };
  checking.add(app.slug);
  emitChecking();
  try {
    return await postCheck(app.slug).catch(
      (e: Error) => ({ status: "error", message: e.message || "Couldn't check for a new release" }) as MarketAnswer,
    );
  } finally {
    await refreshCatalog({ again: true }).catch(() => {});
    checking.delete(app.slug);
    emitChecking();
  }
}

/** Whether a release check for `slug` is in flight. */
export function useChecking(slug: string): boolean {
  const [on, setOn] = useState(() => checking.has(slug));
  useEffect(() => {
    const l = () => setOn(checking.has(slug));
    checkListeners.add(l);
    l();
    return () => {
      checkListeners.delete(l);
    };
  }, [slug]);
  return on;
}

/** The jobs readers see, synchronously (tests). */
export function peekJobs(): Record<string, InstallJob> {
  return visible;
}

/** Every job the server lists. The poll runs only while one is active. */
export function useJobs(): Record<string, InstallJob> {
  const [j, setJ] = useState(visible);
  useEffect(() => {
    jobListeners.add(setJ);
    setJ(visible);
    return () => {
      jobListeners.delete(setJ);
    };
  }, []);
  return j;
}

export function useJob(slug: string): InstallJob | null {
  return useJobs()[slug] ?? null;
}

// -- Derived action state -------------------------------------------------------

/** What an app's primary action is right now — the one rule every pill
 *  (card, hero, installed row) reads, so they can never disagree. */
export type AppActionKind = "get" | "progress" | "open" | "update" | "retry" | "incompatible";

/** The copy on disk is a stage-B reviewed copy (§18.4): its Update is
 *  "Check for a new release", never the old package update. */
export function isMarketCopy(app: AppEntry): boolean {
  return app.installed?.kind === "marketplace";
}

/** A plain-file install from the old Marketplace: it never updates; the
 *  Installed page offers the switch to the reviewed version instead. */
export function isLegacyCopy(app: AppEntry): boolean {
  return app.installed?.kind === "legacy";
}

/** The author's own copy of the listed app: "Your app", Open only. */
export function isOwnCopy(app: AppEntry): boolean {
  return app.installed?.kind === "own";
}

/** A newer reviewed release is waiting for this copy. */
export function releaseWaiting(app: AppEntry): boolean {
  if (!app.installed) return false;
  if (isLegacyCopy(app) || isOwnCopy(app)) return false;
  return !!(app.update_available || app.installed.update_available);
}

export interface AppAction {
  kind: AppActionKind;
  /** 0..1 while `progress`; null = indeterminate (queued). */
  progress: number | null;
  error: string | null;
  /** The install folder, once there is one. */
  path: string | null;
}

export function actionFor(app: AppEntry, job: InstallJob | null): AppAction {
  const path = app.installed?.path ?? job?.path ?? null;
  if (job && isActive(job)) {
    return {
      kind: "progress",
      progress: job.state === "queued" ? null : Math.max(0, Math.min(1, job.progress || 0)),
      error: null,
      path,
    };
  }
  if (job && job.state === "error") {
    return { kind: "retry", progress: null, error: job.error || job.message || "Install failed", path };
  }
  // A job that finished but whose catalog re-read has not landed yet still
  // reads as installed and current — the pill must not flash back to Get
  // (install) or Update (update) in between. Readers only ever see a done
  // job until the catalog has caught up with it (`visibleJobs`), so this
  // never overrides a fresher catalog.
  if (job && job.state === "done") {
    return { kind: "open", progress: null, error: null, path };
  }
  if (app.installed) {
    if (releaseWaiting(app) && app.compatible) return { kind: "update", progress: null, error: null, path };
    return { kind: "open", progress: null, error: null, path };
  }
  if (!app.compatible) return { kind: "incompatible", progress: null, error: null, path: null };
  return { kind: "get", progress: null, error: null, path: null };
}
