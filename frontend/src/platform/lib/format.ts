// Pure formatting helpers. No DOM, no fetch. (The vanilla module also carried
// escapeHtml — dropped: JSX escapes text content itself.)
import { ORIGIN_BY_ROUTE } from "@platform/lib/originRoutes";

// THE CLIENT-SIDE COUNTERPART TO `origin_for_page` (fused_render/jobs.py) —
// found and reused for the SAME reason that function exists (a `.dl-origin`
// caption naming who raised a notification row), not a second,
// independently-invented labeller. `Job.origin` is computed server-side, at
// report time, with a closed shell-route table and a `projectenv`
// project-root lookup neither of which a client-raised message's `notify()`
// call (`platform/lib/notifications.ts`) or a waiting task's own row
// (`shell/tasks-lib.ts`'s `attentionRows`) can reach synchronously — no
// request round trip happens at either call site.
//
// Until this fix this function was basename-only and disagreed, visibly,
// with `origin_for_page` on the very sources it CAN name without a round
// trip: a source of `/tasks` labelled "tasks" here and "Scheduler" there. It
// now consults the SAME shared route table (`ORIGIN_BY_ROUTE`,
// `platform/lib/originRoutes.ts`, which `router.ts` and `jobs.py`'s
// `_ORIGIN_BY_ROUTE` also read/mirror) before falling back to the bare-path
// rule. The one thing this function still cannot replicate is
// `origin_for_page`'s PROJECT-name resolution for an ordinary fs path
// (`projectenv.project_root_for` + `projectenv.display_name`), which needs
// server-side filesystem access no client call site has — that remaining
// divergence (an fs path outside the closed route table labels by basename
// here, by project name there) is deliberate and bounded: every route this
// function CAN name authoritatively, it now names identically to the server.
//
// LIVES HERE, NOT in notifications.ts: `tasks-lib.ts` (shell/) needs it too,
// and notifications.ts imports router.ts, which reads `location` at module
// scope — importing notifications.ts from tasks-lib.ts broke every one of
// its tests that don't install a DOM shim before their own static imports
// evaluate (tasks-lib.test.ts had never needed one). format.ts has no
// imports and no side effects of its own, so both callers can reach this
// without dragging that module-init chain in. `originRoutes.ts` has the same
// property, so importing it here does not change that.
export function labelForSource(source: string | undefined): string {
  const trimmed = (source || "").trim();
  if (!trimmed) return "";
  // The full string is tried FIRST: a query-bearing route can itself be a
  // table key (`"/preferences?tab=indexing"` alongside the bare
  // `"/preferences"`) that is MORE specific than what stripping its query
  // string would leave — strip first and the indexing page mislabels as
  // "Preferences". Only once the full string misses is the query string (and
  // any hash) stripped and tried again, which is what turns a task
  // destination like `/explorer/view/Users/x/app?_side=claude&session_id=…`
  // into a clean route/basename lookup instead of carrying that junk tail
  // into either lookup or the basename fallback below.
  const routed = ORIGIN_BY_ROUTE[trimmed];
  if (routed) return routed;
  const withoutQuery = trimmed.split(/[?#]/)[0];
  const routedStripped = ORIGIN_BY_ROUTE[withoutQuery];
  if (routedStripped) return routedStripped;
  // fs-path fallback: basename, extension stripped. `origin_for_page`'s own
  // documented fallback for the same "not a known route" case — see the file
  // header comment above for why this function stops here rather than also
  // resolving a project name.
  const stripped = withoutQuery.replace(/[/\\]+$/, "");
  const base = stripped.split(/[/\\]/).pop() || stripped;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes;
  let u = -1;
  do {
    v /= 1024;
    u++;
  } while (v >= 1024 && u < units.length - 1);
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[u]}`;
}

// Parameter counts, the unit models are compared in — "7.2B", "465M". Distinct
// from formatSize: these are counts, not bytes, so the steps are decimal (a
// "7B model" is 7e9 parameters, never 7 * 2^30) and the unit is never implied
// by the number alone.
export function formatParams(count: number | null | undefined): string {
  if (!count || count < 0) return "";
  // The threshold is against the ROUNDED value, not the raw one: a count like
  // 999,700,000 is under 1e9 so the naive check sends it through the M step,
  // where `Math.round(999_700_000 / 1e6)` is 1000 — "1000M" rather than the
  // "1.0B" a reader expects the moment a compact count rolls over three
  // digits (`gemma-3-1b-it`'s own real parameter count is exactly this
  // shape). Checking what the M-step would ROUND TO catches that before it
  // renders, without changing anything for a count nowhere near the boundary.
  if (count >= 1e9 || Math.round(count / 1e6) >= 1000) return `${Number((count / 1e9).toFixed(1))}B`;
  if (count >= 1e6) return `${Math.round(count / 1e6)}M`;
  if (count >= 1e3) return `${Math.round(count / 1e3)}K`;
  return `${count}`;
}

// Listing-grade stamp: locale date + hours:minutes. Seconds are noise in a
// column of file dates — and carrying them made MODIFIED the widest column in
// the table, which is backwards for the least important one. The full
// precision is still one hover away (the cells carry formatMtimeFull as their
// title) and one panel away (Preview's stat card uses it outright).
export function formatMtime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

// Full precision, seconds included — for tooltips and the stat panel.
export function formatMtimeFull(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString();
}

// "3d ago" style stamp; null when no time is known.
export function timeAgo(epochSeconds: number | null | undefined): string | null {
  if (!epochSeconds) return null;
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

export function basename(fsPath: string): string {
  const parts = fsPath.split("/").filter((s) => s.length > 0);
  return parts.length ? parts[parts.length - 1] : "/";
}

/** The half of a Hugging Face repo id that names the MODEL: `FLUX.2-klein-4B`
 *  out of `black-forest-labs/FLUX.2-klein-4B`.
 *
 *  Deliberately not `basename()` above, even though both split on "/" — that
 *  one is about filesystem paths and answers `"/"` for a root, which is not a
 *  meaningful model name. An id with no owner prefix (a local alias, a bare
 *  name) passes through untouched, and so does a trailing slash rather than
 *  becoming an empty label.
 *
 *  The owner is dropped for DISPLAY only, never from the value: two models with
 *  the same name under different owners are indistinguishable once shortened,
 *  so every call site keeps the full id in a `title` attribute. */
export function repoName(modelId: string): string {
  const parts = modelId.split("/").filter((s) => s.length > 0);
  return parts.length ? parts[parts.length - 1] : modelId;
}

export function dirname(fsPath: string): string {
  const idx = fsPath.replace(/\/+$/, "").lastIndexOf("/");
  if (idx <= 0) return idx === 0 ? "/" : "";
  return fsPath.slice(0, idx);
}
