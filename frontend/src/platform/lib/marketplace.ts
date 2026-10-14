// The Marketplace's data layer (fused_render/marketplace.py, the
// /api/marketplace/* routes): typed fetchers for the catalog, one app's
// detail, the install jobs and the install POST, plus the two small stores
// the pages share.
//
// CATALOG — a module cache, not a per-mount fetch. Every marketplace route
// remounts on the nav epoch (App.tsx), so a card → detail → Back hop would
// otherwise paint skeletons twice for a list that has not changed. The cache
// answers instantly and a background revalidate (single-flight) lands any
// change; `refreshCatalog({force: true})` asks the server to re-read GitHub.
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
import { useEffect, useState } from "react";
import { getJson, postJson } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { encodeFsPathSegments, navigateUrl, urlForFsPath, viewUrlForFsPath } from "@platform/lib/router";

// -- Wire types (the API contract) -------------------------------------------

export interface MarketplaceAuthor {
  name: string;
  github?: string | null;
}

export interface InstalledStamp {
  path: string;
  /** Null when the stamp predates a version (or could not be read). */
  version: string | null;
  tree_sha: string;
  installed_at: string;
  /** Local files differ from the sha256s stamped at install time. */
  modified: boolean;
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
  tree_sha: string;
  source_url: string | null;
  featured: boolean;
  installed: InstalledStamp | null;
  update_available: boolean;
  compatible: boolean;
}

export interface MarketplaceCategory {
  id: string;
  label: string;
  count: number;
}

export interface Catalog {
  status: "ok" | "error";
  message?: string;
  error?: string;
  source?: "github-api" | "git-mirror";
  repo?: string;
  commit?: string;
  fetched_at?: string;
  stale?: boolean;
  warning?: string | null;
  categories: MarketplaceCategory[];
  apps: AppEntry[];
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

export function fetchApp(slug: string): Promise<AppDetail> {
  return getJson<AppDetail>(`/api/marketplace/app/${encodeURIComponent(slug)}`);
}

export function fetchJobs(): Promise<{ jobs: Record<string, InstallJob> }> {
  return getJson<{ jobs: Record<string, InstallJob> }>("/api/marketplace/jobs");
}

export function fetchInstalled(): Promise<{ apps: AppEntry[] }> {
  return getJson<{ apps: AppEntry[] }>("/api/marketplace/installed");
}

export function postInstall(slug: string): Promise<{ status: "ok"; job: InstallJob }> {
  return postJson<{ status: "ok"; job: InstallJob }>("/api/marketplace/install", { slug });
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
export function useCatalog(): CatalogSnapshot {
  const [s, setS] = useState(snapshot);
  useEffect(() => {
    catalogListeners.add(setS);
    setS(snapshot);
    void refreshCatalog();
    return () => {
      catalogListeners.delete(setS);
    };
  }, []);
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
  try {
    const r = await fetchJobs();
    setJobs(r.jobs ?? {});
  } catch {
    /* A missed poll is retried by the next tick while anything is active. */
  } finally {
    polling = false;
    schedule();
  }
}

/** Start an install (or an update — the server decides from what is on
 *  disk). The returned job is merged in at once so the pill turns into a
 *  ring on the same frame, then the poll takes over. */
export async function installApp(app: { slug: string; name: string }): Promise<InstallJob> {
  startedHere.set(app.slug, app.name);
  announced.delete(app.slug);
  try {
    const r = await postInstall(app.slug);
    setJobs({ ...jobs, [app.slug]: r.job });
    schedule();
    return r.job;
  } catch (e) {
    startedHere.delete(app.slug);
    const message = (e as Error).message || "Install failed";
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
    if (app.update_available && app.compatible) return { kind: "update", progress: null, error: null, path };
    return { kind: "open", progress: null, error: null, path };
  }
  if (!app.compatible) return { kind: "incompatible", progress: null, error: null, path: null };
  return { kind: "get", progress: null, error: null, path: null };
}
