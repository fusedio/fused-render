// Builds (OpenBot src/builds.js): hand a prompt to Claude Code through the tasks API and track it here. One build =
// one Claude task that creates a fused app. State is module-level like OpenBot's; the footer chip's numbers go out
// through the store's setBuildsChip, the panel's iframe is a registered element this module drives directly (as
// OpenBot drove #bframe), and the build dialog is a promise (buildDialog) the BuildDialog component renders.
//
// Differences from builds.js, all forced by the host: builds.json is GET/POST /api/bots/builds (no appDir, no
// .fused/data); the folder is made by POST /api/bot-apps/mkdir (mkbuild.py); fused.tasks.* are builds/tasks.ts.
import { useSyncExternalStore } from "react";
import { api, type BuildRow } from "../lib/api";
import { errMsg, getState, openPanel, closePanel, setBuildsChip, showBanner, showToast } from "../state/store";
import { tasksCreate, tasksList, tasksMarkRead, tasksUi, tasksWatch, type TaskHandle, type TaskRow } from "./tasks";

// ------------------------------------------------------------------ root ----
// <Fused workspace>/app: new apps land here, one folder per build. From /api/config (fused_dir), the same value the
// server derives. Lazy so importing this module (tests) never fetches.
let BUILDS_ROOT = "";
let rootP: Promise<void> | null = null;
const rootSubs = new Set<() => void>();
export const buildsRoot = (): string => BUILDS_ROOT;
export function setBuildsRoot(root: string): void {
  BUILDS_ROOT = root;
  for (const l of [...rootSubs]) l();
}
export function rootReady(): Promise<void> {
  return rootP || (rootP = fetch("/api/config").then((r) => r.json())
    .then((c: { fused_dir?: string } | null) => { if (c && c.fused_dir) setBuildsRoot(`${c.fused_dir}/app`); })
    .catch(() => {}));
}
/** BUILDS_ROOT for React (the panel subtitles); kicks off the /api/config read. */
export function useBuildsRoot(): string {
  return useSyncExternalStore((l) => { rootSubs.add(l); void rootReady(); return () => { rootSubs.delete(l); }; }, buildsRoot, buildsRoot);
}

// A build prompt starts with one of these (old and new form); identifies builds made before builds.json existed.
export const BUILD_MARKS = ["Build \"", "Create a new fused-render app named"];
export const marked = (s: unknown): boolean => BUILD_MARKS.some((m) => String(s || "").startsWith(m));
/** OpenBot: local, not a preview, fused.tasks present. This page is always local and never a preview. */
export const buildsOn = (): boolean => true;

let builds: BuildRow[] = [];
let buildRows: TaskRow[] = [];
let buildsLoaded = false;
let buildFrameFor = "", buildsUiUrl = "";
/** entryId → handle, this page load only (handles do not survive a reload). */
const buildHandles: Record<string, TaskHandle> = {};
export const getBuilds = (): BuildRow[] => builds;
export const getBuildRows = (): TaskRow[] => buildRows;
/** Tests: seed the module state. */
export function _setBuildsState(b: BuildRow[], rows: TaskRow[]): void { builds = b; buildRows = rows; adoptSeen = null; }
/** Tests: a feed tick (rows move; builds and the adopt "seen" set stay). */
export function _setRowsOnly(rows: TaskRow[]): void { buildRows = rows; }

export const slugOf = (s: unknown): string =>
  String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "app";

export async function loadBuilds(): Promise<void> {
  await rootReady();
  // The machine-wide Tasks UI is what the panel frames (builds live in other folders).
  try { buildsUiUrl = await tasksUi({ view: "list", scope: "all" }); } catch { buildsUiUrl = ""; }
  try { builds = (await api.builds()).builds || []; } catch { builds = []; }
  buildsLoaded = true;
}
/** OpenBot's `if (!appDir) return`: never write builds.json before it was read. */
export async function saveBuilds(): Promise<void> {
  if (!buildsLoaded) return;
  try { await api.saveBuilds(builds); } catch (e) { console.warn("builds.json", e); }
}

const panelShown = (): boolean => getState().ui.panel === "builds";

// A task is a build when we recorded it, or when its prompt carries the build marker (adopted into builds.json on sight).
// A row without an entry id (a bare session) must never match a recorded build whose id is "" — seen live: one such
// record made EVERY session row a build, and the panel showed the whole task list.
const sameEntry = (b: BuildRow, t: TaskRow): boolean => !!t.entry_id && b.entryId === t.entry_id;
export const isBuild = (t: TaskRow): boolean =>
  builds.some((b) => sameEntry(b, t)) || marked(t.title) || (t.messages || []).some((m) => marked(m.body));
export const mine = (): TaskRow[] => buildRows.filter(isBuild);
/** The name/folder a build prompt (or its title) carries. */
export const BUILD_RE = /(?:named|Build) "([^"]+)"(?: in the folder | · (?:new fused-render app|update to the fused-render app) in )(\S+)/;
let adoptSeen: Set<string> | null = null;
/** Returns whether builds changed (and saves them when they did). */
export function adoptBuilds(open: boolean = panelShown()): boolean {
  let changed = false;
  // Anything that appeared while the Builds panel was open was started from here (its New task button or ours): count it as ours.
  const seen = adoptSeen; adoptSeen = new Set(buildRows.map((t) => t.entry_id || ""));
  const near = (t: TaskRow) => { const f = String(t.project || t.folder || t.cwd || ""); return !f || f.startsWith(BUILDS_ROOT); };  // scope "all" sees every folder; only adopt nearby rows
  if (seen && open) for (const t of buildRows) if (!seen.has(t.entry_id || "") && !isBuild(t) && near(t)) {
    builds.push({ entryId: t.entry_id || "", name: t.title || "Task", dir: "", createdAt: Date.now() }); changed = true;
  }
  for (const t of mine()) if (!builds.some((b) => sameEntry(b, t))) {
    const m = BUILD_RE.exec([t.title, ...(t.messages || []).map((x) => x.body)].join("\n")) || [];
    builds.push({ entryId: t.entry_id || "", name: m[1] || t.title || "Build", dir: m[2] || "", createdAt: (t.started || Date.now() / 1000) * 1000 }); changed = true;
  }
  if (changed) void saveBuilds();
  return changed;
}

// The Claude session runs inside the new app's folder (create() gets target = dir, made first by /api/bot-apps/mkdir), so
// it picks up that folder's own context and writes there without leaving its working directory. Because the task then
// belongs to the new app, every list/watch/ui call uses scope "all" and filters by builds.json.
export function buildPrompt(name: string, dir: string, ask: string): string {
  // First line doubles as the task's title in the shell's list, so it stays short.
  return `Build "${name}" · new fused-render app in ${dir}

You are running inside ${dir}, an empty folder made for this app. Build the app there.
Rules:
- Invoke the fused-render-authoring skill before writing any code and follow its contract.
- Exactly one entry page, ${dir}/index.html, with <meta name="fused-app" /> and <meta name="fused-api-version" content="1" /> near the top of <head>.
- Plain HTML/CSS/JS, no build step, no network at runtime. Python beside the page via fused.runPython only when it adds value, with a pyproject.toml in that folder.
- Every .py beside the page exposes ONE top-level annotated main(**params), returns JSON-native values, takes no argv/stdin and finishes under 60 s, and gets a section in the app's SKILL.md (beside index.html): what it does, what it changes, args, return shape, one example call. Bots read that SKILL.md to call these files directly (their \`py\` action). Keep SKILL.md in step with every .py you add, change or remove. The authoring skill's "App SKILL.md" section has the exact format.
- Follow the shell theme (data-fused-theme="shell") and gate the _preview=1 mode.
- ALL UI state MUST live in the URL through fused.params (selected tab, filters, search text, sort, open item, map view, toggles, any value the user picks): read it on load, write it on every change, never keep view state only in JS variables or localStorage. The shell's Copy state button copies the page URL, so a copied link must reopen the app exactly as the user sees it.
- Add a short README.md describing the app. Do not touch anything outside ${dir}.
- When done, reply with a two-line summary and the folder path.

What the app should do:
${ask.trim()}`;
}

// A task that edits an app that already exists. Same shape as buildPrompt so adoptBuilds' regex recognises it.
export function updatePrompt(name: string, dir: string, ask: string): string {
  return `Build "${name}" · update to the fused-render app in ${dir}

You are running inside ${dir}, an existing fused-render app. Read its README.md, CLAUDE.md and index.html first, then make the change below in place.
Rules:
- Invoke the fused-render-authoring skill before writing any code and follow its contract.
- Keep the single entry page ${dir}/index.html with its <meta name="fused-app" /> and <meta name="fused-api-version" /> tags.
- Plain HTML/CSS/JS, no build step, no network at runtime. Python beside the page via fused.runPython only when it adds value.
- Keep ALL UI state in the URL through fused.params, as the app already does; do not move state into JS variables or localStorage.
- Keep the app's existing look and behaviour except where the change says otherwise. Update README.md if the change alters what the app does. Do not touch anything outside ${dir}.
- When done, reply with a two-line summary of what changed.

The change:
${ask.trim()}`;
}

// ------------------------------------------------------------------ panel: the shell's Tasks UI in an iframe ----
let frameEl: HTMLIFrameElement | null = null, pendingSrc = "";
/** BuildsPanel's #bframe ref. */
export function setBuildFrame(el: HTMLIFrameElement | null): void {
  frameEl = el;
  if (el && pendingSrc) { el.src = pendingSrc; pendingSrc = ""; }
}
function setFrameSrc(src: string) { if (frameEl) frameEl.src = src; else pendingSrc = src; }

export async function showBuildFrame(taskKey?: string): Promise<void> {
  const want = taskKey || "list";
  if (buildFrameFor === want) return;
  buildFrameFor = want;
  try {
    setFrameSrc(taskKey ? await tasksUi({ task: taskKey, scope: "all" }) : buildsUiUrl || await tasksUi({ view: "list", scope: "all" }));
  } catch (e) { buildFrameFor = ""; console.warn("builds ui", e); }
}
// Opened from code (a new build/task) rather than the footer chip: the chip keeps its `fresh` mark.
let openedByCode = false;
/** BuildsPanel, when the panel opens: true when openBuilds() opened it (consumes the flag). */
export function takeOpenedByCode(): boolean { const v = openedByCode; openedByCode = false; return v; }
export function openBuilds(taskKey?: string): void {
  if (!buildsOn()) return;
  if (!panelShown()) openedByCode = true;
  openPanel("builds");
  void showBuildFrame(taskKey);
}
export function closeBuilds(): void { if (panelShown()) closePanel(); }

/** The row-filter stylesheet for the framed Tasks list (pure, for tests). */
export function buildFilterCss(keys: Iterable<string>): string {
  const sel = [...keys].map((k) => `.tasks-row[data-peek-key="${k.replace(/"/g, "")}"]`);
  const hideRows = sel.length ? `.tasks-node${sel.map((s) => `:not(:has(${s}))`).join("")}` : ".tasks-node";
  const empty = `.tasks-list-frame${sel.length ? `:not(:has(${sel.join(", ")}))` : ""}::before { content: "No builds yet. Start one with New build."; display: block; padding: 40px 16px; text-align: center; opacity: .6; }`;
  // With the Board/Cards switch gone the search, filters and New task button would sit at the left edge; push them to the right, and keep the search box from being squeezed.
  const toolbar = `.schedule-toolbar { justify-content: flex-end !important; } .schedule-toolbar .schedule-tv-filters { margin-left: auto !important; } .schedule-toolbar .schedule-tv-search { flex: 0 1 260px !important; min-width: 140px !important; }`;
  // Embed mode drops the page's side gutter and lets the list run off the left edge; give the page a real frame: even padding all
  // round, no stray "Tasks" heading (our top bar already says Builds), rows a touch roomier, and nothing wider than the frame.
  const layout = `.schedule-page { padding: 18px 24px 24px !important; gap: 14px !important; margin: 0 !important; max-width: none !important; width: 100% !important; box-sizing: border-box !important; }
    .schedule-page > * { max-width: none !important; min-width: 0 !important; margin-inline: 0 !important; width: 100% !important; box-sizing: border-box !important; }
    .schedule-page > .schedule-header { display: none !important; }
    .schedule-page .schedule-main > .tasks-list { border: 1px solid var(--border) !important; border-radius: 10px !important; min-width: 0 !important; width: 100% !important; box-sizing: border-box !important; }
    .tasks-list > * { min-width: 0 !important; }
    .tasks-node { --tasks-row-pad: 18px; --tasks-row-pad-y: 12px; }`;
  return `${hideRows} { display: none !important; } .schedule-view-seg, .task-side-peek-acts button[aria-label="Previous task"], .task-side-peek-acts button[aria-label="Next task"] { display: none !important; } ${toolbar} ${layout} ${empty}`;
}

// The shell's list shows every task. It is same-origin, so a stylesheet dropped into it keeps only build rows (matched
// on the row's data-peek-key = task key), and hides its Board/Cards switch, which this filter does not cover. Its New
// task button stays: a task created while this panel is open is adopted (see adoptBuilds), so it does not vanish from
// the filtered list. Class names are the shell's; if they change the filter simply stops applying and the full list shows.
export function applyBuildFilter(): void {
  let d: Document | null = null;
  try { d = frameEl?.contentDocument || null; } catch { return; }
  if (!d || !d.head) return;
  const keys = new Set(mine().map((t) => t.key));
  for (const h of Object.values(buildHandles)) keys.add(h.key);
  const css = buildFilterCss(keys);
  let s = d.getElementById("obfilter");
  if (!s) { s = d.createElement("style"); s.id = "obfilter"; d.head.appendChild(s); }
  if (s.textContent !== css) s.textContent = css;
}

/** The chip numbers (pure, for tests). */
export function chipFor(rows: TaskRow[]): { n: string; live: boolean; warn: boolean; title: string } {
  const live = rows.filter((t) => ["in_progress", "queued", "upcoming"].includes(t.status)).length;
  const wait = rows.filter((t) => ["needs_attention", "blocked"].includes(t.status)).length;
  return {
    n: live + wait ? String(live + wait) : "",
    warn: wait > 0,
    live: live > 0 && !wait,
    title: wait ? `${wait} build${wait > 1 ? "s" : ""} need${wait > 1 ? "" : "s"} your attention` : live ? `${live} build${live > 1 ? "s" : ""} running` : "Builds · Claude tasks that create fused apps",
  };
}

// Footer chip: how many builds are running or waiting on you; amber when one needs attention.
export function renderBuildChip(): void {
  adoptBuilds();
  setBuildsChip(chipFor(mine()));
  applyBuildFilter();
}

// ------------------------------------------------------------------ new build dialog (#bdmodal) ----
export interface BuildDialogApp { name?: string; folder?: string; dir: string }
export interface BuildDialogResult { name: string; prompt: string; model: string; effort: string; permissionMode: string }
export interface BuildDialogReq { app: BuildDialogApp | null; seq: number; resolve: (v: BuildDialogResult | null) => void }
let dialogReq: BuildDialogReq | null = null, dialogSeq = 0;
const dialogSubs = new Set<() => void>();
const dialogEmit = () => { for (const l of [...dialogSubs]) l(); };
/** The open request (BuildDialog renders it). */
export const getBuildDialog = (): BuildDialogReq | null => dialogReq;
export const isBuildDialogOpen = (): boolean => !!dialogReq;
export function useBuildDialog(): BuildDialogReq | null {
  return useSyncExternalStore((l) => { dialogSubs.add(l); return () => { dialogSubs.delete(l); }; }, getBuildDialog, getBuildDialog);
}
/** With `app` ({name, dir}) the same dialog asks for a change to that existing app: the name and folder are fixed. */
export function buildDialog(app?: BuildDialogApp | null): Promise<BuildDialogResult | null> {
  if (dialogReq) dialogReq.resolve(null);
  return new Promise((resolve) => {
    dialogReq = {
      app: app || null, seq: ++dialogSeq,
      resolve: (v) => { dialogReq = null; dialogEmit(); resolve(v); },
    };
    dialogEmit();
  });
}

// ------------------------------------------------------------------ starting builds ----
// "New task" from the app viewer: a Claude session in the app's own folder, listed with the builds.
export async function newAppTask(app: BuildDialogApp | null | undefined): Promise<void> {
  if (!app?.dir) return;
  const v = await buildDialog(app); if (!v) return;
  const name = app.name || app.folder || app.dir.split("/").pop() || "App";
  let h: TaskHandle;
  try {
    h = await tasksCreate({ prompt: updatePrompt(name, app.dir, v.prompt), target: app.dir, title: `Task · ${name}`, model: v.model, effort: v.effort, permissionMode: v.permissionMode });
  } catch (e) { showBanner("Could not start the task: " + errMsg(e)); return; }
  builds.push({ entryId: h.entryId, name, dir: app.dir, createdAt: Date.now() });
  buildHandles[h.entryId] = h;
  await saveBuilds();
  openBuilds(h.key);
  applyBuildFilter();
  followBuild(h, name, app.dir);
}

export async function newBuild(): Promise<void> {
  const v = await buildDialog(); if (!v) return;
  await rootReady();
  const dir = `${BUILDS_ROOT}/${slugOf(v.name)}`;
  let h: TaskHandle;
  try {
    await api.mkdirApp(dir);  // target must exist before the session starts there
    h = await tasksCreate({ prompt: buildPrompt(v.name, dir, v.prompt), target: dir, title: `Build · ${v.name}`, model: v.model, effort: v.effort, permissionMode: v.permissionMode });
  } catch (e) { showBanner("Could not start the build: " + errMsg(e)); return; }
  builds.push({ entryId: h.entryId, name: v.name, dir, createdAt: Date.now() });
  buildHandles[h.entryId] = h;
  await saveBuilds();
  openBuilds(h.key);
  applyBuildFilter();
  followBuild(h, v.name, dir);
}

// h.done follows the pending→session rekey, so this is the one safe place to fire a one-shot "finished" note.
export function followBuild(h: TaskHandle, name: string, _dir: string): void {
  h.done.then(async () => {
    const b = builds.find((x) => x.entryId === h.entryId); if (b) { b.doneAt = Date.now(); void saveBuilds(); }
    await tasksMarkRead(h.key).catch(() => {});
    showToast({ text: `Build finished: ${name}`, ts: Date.now() / 1000 });
    if (!panelShown()) setBuildsChip({ fresh: true });
  }).catch(() => {});
}

// ------------------------------------------------------------------ boot ----
/** Load builds.json, list once, then ride the shared long poll (one per document). Returns the teardown. Call once (BuildsPanel). */
export function startBuilds(): () => void {
  if (!buildsOn()) { setBuildsChip({ hidden: true }); return () => {}; }
  let alive = true;
  loadBuilds().then(() => tasksList({ scope: "all" })).then((rows) => { if (alive) { buildRows = rows; renderBuildChip(); } }).catch((e) => console.warn("builds", e));
  const unwatch = tasksWatch((rows) => { buildRows = rows; renderBuildChip(); }, { scope: "all" });
  const onHide = () => unwatch();
  addEventListener("pagehide", onHide);
  return () => { alive = false; unwatch(); removeEventListener("pagehide", onHide); };
}
